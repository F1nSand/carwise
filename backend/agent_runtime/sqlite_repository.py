"""SQLite WAL repositories and store-shaped compatibility adapters."""

from __future__ import annotations

import json
import hashlib
import hmac
import secrets
import sqlite3
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

from backend.agent_runtime.run_store import _redact, _validate_run_id, _TERMINAL, canonical_request_hash
from backend.agent_runtime.run_contracts import validate_display_result, validate_pending_input
from backend.agent_runtime.session_store import (
    SessionSnapshot,
    SessionStore,
    _validate_session_id,
)
from backend.api_projection import public_run_event, public_terminal_result
from backend.agent_runtime.runtime_projection import runtime_from_checkpoint
from backend.config import RUNTIME_DATABASE_PATH


def classify_sqlite_failure(exc: Exception) -> tuple[str, bool] | None:
    """Classify SQLite primary/extended result codes without parsing exception text."""
    code = getattr(exc, "sqlite_errorcode", None)
    if not isinstance(exc, sqlite3.Error) or not isinstance(code, int):
        return None
    primary = code & 0xFF
    names = {sqlite3.SQLITE_BUSY: "storage_busy", sqlite3.SQLITE_LOCKED: "storage_locked",
             sqlite3.SQLITE_READONLY: "storage_readonly", sqlite3.SQLITE_CORRUPT: "storage_corrupt",
             sqlite3.SQLITE_NOTADB: "storage_corrupt", sqlite3.SQLITE_IOERR: "storage_io_error"}
    return names.get(primary, "storage_failure"), primary in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}


def _now() -> str:
    return datetime.now(UTC).isoformat()


_RUNTIME_PROJECTION_KEYS = {
    "status", "plan", "activities", "task_intent", "task_mode", "task_revision",
    "plan_revision", "current_stage", "pending_input", "input_revision", "capabilities",
    "last_event_seq", "state_revision",
}


def _runtime_for_result(
    result: dict[str, Any] | None,
    previous: Any,
    *,
    status: str,
    state_revision: int,
) -> dict[str, Any]:
    candidate: Any = None
    if isinstance(result, dict):
        candidate = result.get("runtime") or result.get("state")
    if isinstance(candidate, dict) and ("flags" in candidate or "run_logs" in candidate):
        return runtime_from_checkpoint(
            candidate,
            status=status,
            state_revision=state_revision,
            capabilities=candidate.get("capabilities") if isinstance(candidate.get("capabilities"), dict) else None,
        )
    source = candidate if isinstance(candidate, dict) else previous
    runtime = {key: value for key, value in source.items() if key in _RUNTIME_PROJECTION_KEYS} if isinstance(source, dict) else {}
    runtime["status"] = status
    runtime["state_revision"] = max(0, state_revision)
    intent = runtime.get("task_intent")
    if isinstance(intent, dict):
        runtime.setdefault("task_revision", intent.get("revision", 0))
    return runtime


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _load_json(value: str | None, default: Any = None) -> Any:
    if value is None:
        return deepcopy(default)
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("SQLite runtime JSON 字段格式非法") from exc


def _safe_execution_group_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Persist only the compact, public parts of a historical execution group."""
    result: dict[str, Any] = {}
    for key, limit in (("title", 120), ("summary", 500), ("started_at", 64), ("finished_at", 64)):
        value = payload.get(key)
        if isinstance(value, str):
            result[key] = value[:limit]
    status = payload.get("status")
    if isinstance(status, str) and status in {"running", "complete", "error", "idle"}:
        result["status"] = status
    duration = payload.get("duration_ms")
    if isinstance(duration, int) and not isinstance(duration, bool):
        result["duration_ms"] = max(0, duration)

    activities: list[dict[str, Any]] = []
    for raw in payload.get("activities", [])[:50] if isinstance(payload.get("activities"), list) else []:
        if not isinstance(raw, dict):
            continue
        item: dict[str, Any] = {}
        for key, limit in (("id", 128), ("tool_name", 128), ("plan_step_id", 64), ("detail", 500)):
            value = raw.get(key)
            if isinstance(value, str):
                item[key] = value[:limit]
        activity_status = raw.get("status")
        if not isinstance(activity_status, str) or activity_status not in {"running", "waiting", "complete", "error"}:
            continue
        item["status"] = activity_status
        output = raw.get("output")
        if isinstance(output, dict):
            safe_output = {
                key: value[:500] if isinstance(value, str) else value
                for key, value in output.items()
                if key in {"ok", "note", "error", "error_code"}
                and (isinstance(value, (str, bool)) or value is None)
            }
            if safe_output:
                item["output"] = safe_output
        duration = raw.get("duration_ms")
        if isinstance(duration, int) and not isinstance(duration, bool):
            item["duration_ms"] = max(0, duration)
        activities.append(item)
    if activities:
        result["activities"] = activities

    plan: list[dict[str, Any]] = []
    for raw in payload.get("plan", [])[:20] if isinstance(payload.get("plan"), list) else []:
        if not isinstance(raw, dict):
            continue
        item = {
            key: raw[key][:limit]
            for key, limit in (("id", 64), ("title", 120), ("description", 300), ("kind", 32),
                               ("completion_criteria", 32), ("reason", 500))
            if isinstance(raw.get(key), str)
        }
        if isinstance(raw.get("status"), str) and raw["status"] in {"queued", "running", "complete", "skipped"}:
            item["status"] = raw["status"]
        for key in ("tool_names", "goal_ids"):
            values = raw.get(key)
            if isinstance(values, list):
                item[key] = [value[:80] for value in values[:12] if isinstance(value, str)]
        if item:
            plan.append(item)
    if plan:
        result["plan"] = plan
    return result


class SQLiteRuntimeRepository:
    """Transactional runtime store shared safely by multiple app workers."""

    def __init__(self, path: Path | str | None = None, busy_timeout_ms: int = 5000) -> None:
        self.path = Path(path) if path is not None else RUNTIME_DATABASE_PATH
        if busy_timeout_ms <= 0:
            raise ValueError("busy_timeout_ms 必须为正数")
        self.busy_timeout_ms = int(busy_timeout_ms)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            str(self.path),
            timeout=self.busy_timeout_ms / 1000,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        return connection

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            has_legacy_runs = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='runs'"
            ).fetchone() is not None
            if schema_version < 10 and has_legacy_runs:
                has_rows = connection.execute("SELECT 1 FROM runs LIMIT 1").fetchone() is not None
                if has_rows:
                    backup_path = self.path.with_name(
                        f"{self.path.name}.pre-v10-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.bak"
                    )
                    backup = sqlite3.connect(str(backup_path))
                    try:
                        connection.backup(backup)
                    finally:
                        backup.close()
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    owner_id TEXT,
                    session_id TEXT NOT NULL,
                    context_id TEXT NOT NULL,
                    input_text TEXT NOT NULL,
                    context_revision INTEGER NOT NULL DEFAULT 0,
                    state_revision INTEGER NOT NULL DEFAULT 0,
                    request_json TEXT,
                    status TEXT NOT NULL,
                    next_seq INTEGER NOT NULL DEFAULT 1,
                    covered_event_seq INTEGER NOT NULL DEFAULT 0,
                    resume_revision INTEGER NOT NULL DEFAULT 0,
                    result_json TEXT,
                    error TEXT,
                    checkpoint_json TEXT,
                    pending_input_json TEXT,
                    runtime_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    queued_at TEXT,
                    started_at TEXT,
                    heartbeat_at TEXT,
                    finished_at TEXT,
                    attempt INTEGER NOT NULL DEFAULT 0,
                    worker_id TEXT,
                    lease_token_hash TEXT,
                    lease_expires_at TEXT,
                    failure_code TEXT,
                    retryable INTEGER NOT NULL DEFAULT 0,
                    cancel_requested_at TEXT,
                    version INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_runs_owner_session_updated
                    ON runs(owner_id, session_id, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_runs_status_lease
                    ON runs(status, lease_expires_at);
                CREATE TABLE IF NOT EXISTS run_events (
                    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                    seq INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(run_id, seq)
                );
                CREATE TABLE IF NOT EXISTS run_inputs (
                    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                    client_message_id TEXT NOT NULL,
                    content TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'received',
                    source_report_id TEXT,
                    PRIMARY KEY(run_id, client_message_id)
                );
                CREATE TABLE IF NOT EXISTS client_requests (
                    owner_key TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    client_request_id TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                    response_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(owner_key, operation, client_request_id)
                );
                CREATE TABLE IF NOT EXISTS admission_usage (
                    run_id TEXT PRIMARY KEY REFERENCES runs(run_id) ON DELETE CASCADE,
                    owner_id TEXT NOT NULL,
                    usage_day TEXT NOT NULL,
                    reserved_tokens INTEGER NOT NULL DEFAULT 0,
                    actual_tokens INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_admission_day ON admission_usage(usage_day, owner_id);
                CREATE INDEX IF NOT EXISTS idx_run_events_run_seq ON run_events(run_id, seq);
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    owner_id TEXT,
                    active_head_id TEXT,
                    active_run_id TEXT,
                    revision INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_run_id TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_sessions_owner_updated
                    ON sessions(owner_id, updated_at DESC);
                CREATE TABLE IF NOT EXISTS session_messages (
                    message_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
                    parent_id TEXT REFERENCES session_messages(message_id) DEFERRABLE INITIALLY DEFERRED,
                    ordinal INTEGER NOT NULL,
                    run_id TEXT,
                    created_at TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    record_json TEXT NOT NULL,
                    UNIQUE(session_id, ordinal)
                );
                CREATE INDEX IF NOT EXISTS idx_session_messages_session_ordinal
                    ON session_messages(session_id, ordinal);
                CREATE INDEX IF NOT EXISTS idx_session_messages_session_run
                    ON session_messages(session_id, run_id);
                CREATE TABLE IF NOT EXISTS legacy_sources (
                    source_key TEXT PRIMARY KEY,
                    content_hash TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    imported_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS conversation_entries (
                    entry_id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
                    conversation_seq INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    run_id TEXT,
                    turn_id TEXT,
                    source_key TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(conversation_id, source_key),
                    UNIQUE(conversation_id, conversation_seq)
                );
                CREATE INDEX IF NOT EXISTS idx_conversation_entries_page
                    ON conversation_entries(conversation_id, conversation_seq DESC);
                CREATE TABLE IF NOT EXISTS conversation_commands (
                    owner_key TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    command_id TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    conversation_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
                    accepted_kind TEXT NOT NULL,
                    run_id TEXT,
                    response_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(owner_key, operation, command_id)
                );
                CREATE TABLE IF NOT EXISTS report_artifacts (
                    report_id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
                    origin_run_id TEXT NOT NULL UNIQUE REFERENCES runs(run_id) ON DELETE CASCADE,
                    version INTEGER NOT NULL,
                    parent_report_id TEXT REFERENCES report_artifacts(report_id),
                    report_schema_version INTEGER NOT NULL,
                    content_json TEXT NOT NULL,
                    context_text TEXT NOT NULL,
                    evidence_manifest_json TEXT NOT NULL DEFAULT '{}',
                    dataset_version TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(conversation_id, version)
                );
                CREATE INDEX IF NOT EXISTS idx_report_artifacts_conversation
                    ON report_artifacts(conversation_id, version DESC);
                """
            )
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(runs)").fetchall()}
            if "request_json" not in columns:
                connection.execute("ALTER TABLE runs ADD COLUMN request_json TEXT")
            if "context_revision" not in columns:
                connection.execute("ALTER TABLE runs ADD COLUMN context_revision INTEGER NOT NULL DEFAULT 0")
            if "state_revision" not in columns:
                connection.execute("ALTER TABLE runs ADD COLUMN state_revision INTEGER NOT NULL DEFAULT 0")
            if "covered_event_seq" not in columns:
                connection.execute("ALTER TABLE runs ADD COLUMN covered_event_seq INTEGER NOT NULL DEFAULT 0")
            input_columns = {row["name"] for row in connection.execute("PRAGMA table_info(run_inputs)").fetchall()}
            if "source_report_id" not in input_columns:
                connection.execute("ALTER TABLE run_inputs ADD COLUMN source_report_id TEXT")
            command_columns = {row["name"] for row in connection.execute("PRAGMA table_info(client_requests)").fetchall()}
            if "response_json" not in command_columns:
                connection.execute("ALTER TABLE client_requests ADD COLUMN response_json TEXT NOT NULL DEFAULT '{}'")
            session_columns = {row["name"] for row in connection.execute("PRAGMA table_info(sessions)").fetchall()}
            if "active_run_id" not in session_columns:
                connection.execute("ALTER TABLE sessions ADD COLUMN active_run_id TEXT")
            for name, definition in (
                ("title", "TEXT NOT NULL DEFAULT ''"),
                ("title_source", "TEXT NOT NULL DEFAULT 'generated'"),
                ("selected_report_id", "TEXT"),
                ("conversation_revision", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if name not in session_columns:
                    connection.execute(f"ALTER TABLE sessions ADD COLUMN {name} {definition}")
            run_columns = {row["name"] for row in connection.execute("PRAGMA table_info(runs)").fetchall()}
            for name, definition in (("turn_id", "TEXT"), ("source_report_id", "TEXT")):
                if name not in run_columns:
                    connection.execute(f"ALTER TABLE runs ADD COLUMN {name} {definition}")
            connection.execute("BEGIN IMMEDIATE")
            try:
                # A context with rows owned by multiple principals cannot be
                # safely assigned to either one. Preserve it but leave it
                # unclaimed so conversation APIs cannot expose mixed history.
                connection.execute(
                    """UPDATE sessions SET owner_id=NULL WHERE session_id IN (
                       SELECT context_id FROM runs WHERE context_id<>''
                       GROUP BY context_id HAVING COUNT(DISTINCT COALESCE(owner_id,''))>1)"""
                )
                # Existing model contexts are conversations. Do not group by
                # the legacy browser grouping key (`runs.session_id`).
                connection.execute(
                    """INSERT OR IGNORE INTO sessions(session_id,owner_id,revision,created_at,updated_at,last_run_id)
                       SELECT context_id, MIN(owner_id), 0, MIN(created_at), MAX(updated_at),
                              (SELECT r2.run_id FROM runs r2 WHERE r2.context_id=runs.context_id
                               ORDER BY r2.updated_at DESC,r2.run_id DESC LIMIT 1)
                       FROM runs WHERE context_id<>'' GROUP BY context_id
                       HAVING COUNT(DISTINCT COALESCE(owner_id,''))=1"""
                )
                connection.execute(
                    """UPDATE sessions SET title=COALESCE(NULLIF(title,''),
                       (SELECT substr(r.input_text,1,80) FROM runs r WHERE r.context_id=sessions.session_id
                        AND (r.owner_id=sessions.owner_id OR (r.owner_id IS NULL AND sessions.owner_id IS NULL))
                        ORDER BY r.created_at,r.run_id LIMIT 1), '新会话')"""
                )
                connection.execute(
                    """UPDATE sessions SET active_run_id=(SELECT r.run_id FROM runs r
                       WHERE r.context_id=sessions.session_id
                       AND (r.owner_id=sessions.owner_id OR (r.owner_id IS NULL AND sessions.owner_id IS NULL))
                       AND r.status IN ('queued','running','waiting_user')
                       ORDER BY r.created_at DESC,r.run_id DESC LIMIT 1)
                       WHERE active_run_id IS NULL AND owner_id IS NOT NULL"""
                )
                self._backfill_legacy_conversation_data(connection)
                connection.execute("PRAGMA user_version=10")
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        finally:
            connection.close()

    def _backfill_legacy_conversation_data(self, connection: sqlite3.Connection) -> None:
        """Backfill old runs as stable chat entries and immutable report artifacts."""
        rows = connection.execute(
            """SELECT r.run_id,r.owner_id,r.context_id,r.input_text,r.status,r.result_json,r.error,
                      r.failure_code,r.retryable,r.created_at,r.updated_at,r.source_report_id,r.runtime_json
               FROM runs r JOIN sessions s ON s.session_id=r.context_id
               WHERE r.context_id<>''
                 AND (r.owner_id=s.owner_id OR (r.owner_id IS NULL AND s.owner_id IS NULL))
               ORDER BY r.context_id,r.created_at,r.run_id"""
        ).fetchall()
        for row in rows:
            run_id = str(row["run_id"])
            context_id = str(row["context_id"])
            now = str(row["updated_at"] or row["created_at"] or _now())
            self._append_conversation_entry_in_transaction(
                connection, context_id, "user_message", f"legacy:{run_id}:user",
                {"text": str(row["input_text"] or ""), "delivery_status": "accepted"},
                run_id=run_id, now=now,
            )
            execution_key = f"run:{run_id}:execution"
            execution_exists = connection.execute(
                "SELECT 1 FROM conversation_entries WHERE conversation_id=? AND source_key=?",
                (context_id, execution_key),
            ).fetchone()
            if execution_exists is None:
                execution_group = self._execution_group_payload(
                    connection, run_id,
                    {"created_at": row["created_at"], "started_at": row["created_at"],
                     "runtime": _load_json(row["runtime_json"], {})},
                    status=str(row["status"] or "error"), finished_at=now,
                )
                if execution_group is not None:
                    self._append_conversation_entry_in_transaction(
                        connection, context_id, "execution_group", execution_key,
                        execution_group, run_id=run_id, now=now,
                    )
            result: dict[str, Any] = {}
            try:
                loaded = _load_json(row["result_json"], {}) or {}
                result = loaded if isinstance(loaded, dict) else {}
            except RuntimeError:
                # Keep the user's question and status even if a legacy result
                # was corrupt; never manufacture a successful report entry.
                result = {}
            status = str(row["status"] or "")
            if status != "done":
                self._append_conversation_entry_in_transaction(
                    connection, context_id, "run_status", f"legacy:{run_id}:status",
                    {"status": status or "error", "message": str(row["error"] or ""),
                     "failure_code": str(row["failure_code"] or ""),
                     "retryable": bool(row["retryable"])},
                    run_id=run_id, now=now,
                )
                continue
            try:
                validate_display_result(result)
            except Exception:
                continue
            public = public_terminal_result(result) or {}
            report = public.get("report") if isinstance(public.get("report"), dict) else None
            final_message = public.get("final_message") if isinstance(public.get("final_message"), dict) else {}
            answer = str(public.get("summary") or final_message.get("content") or "")
            if not answer and isinstance(report, dict):
                answer = str(report.get("answer") or report.get("summaryText") or "")
            if answer:
                self._append_conversation_entry_in_transaction(
                    connection, context_id, "assistant_message", f"legacy:{run_id}:assistant",
                    {"text": answer, "format": "markdown"}, run_id=run_id, now=now,
                )
            if not isinstance(report, dict):
                continue
            existing = connection.execute(
                "SELECT report_id,version FROM report_artifacts WHERE origin_run_id=?", (run_id,),
            ).fetchone()
            if existing:
                report_id, version = str(existing["report_id"]), int(existing["version"])
            else:
                report_id = str(report.get("id") or run_id)
                version = int(connection.execute(
                    "SELECT COALESCE(MAX(version),0)+1 FROM report_artifacts WHERE conversation_id=?",
                    (context_id,),
                ).fetchone()[0])
                parent_id = str(row["source_report_id"] or "") or None
                parent = connection.execute(
                    "SELECT report_id FROM report_artifacts WHERE report_id=? AND conversation_id=?",
                    (parent_id, context_id),
                ).fetchone() if parent_id else None
                parent_id = str(parent["report_id"]) if parent else None
                evidence = report.get("answerEvidence") if isinstance(report.get("answerEvidence"), dict) else {}
                from backend.agent_runtime.report_projection import report_context_text
                context_text = report_context_text(
                    report, answer=answer, report_id=report_id, version=version, origin_run_id=run_id,
                )
                connection.execute(
                    """INSERT OR IGNORE INTO report_artifacts(report_id,conversation_id,origin_run_id,version,
                       parent_report_id,report_schema_version,content_json,context_text,evidence_manifest_json,
                       dataset_version,created_at) VALUES(?,?,?,?,?,1,?,?,?,?,?)""",
                    (report_id, context_id, run_id, version, parent_id, _dump(report), context_text,
                     _dump(evidence), evidence.get("datasetVersion"), now),
                )
            self._append_conversation_entry_in_transaction(
                connection, context_id, "report_card", f"legacy:{run_id}:report",
                {"report_id": report_id, "version": version, "origin_run_id": run_id,
                 "title": str(report.get("summaryTitle") or report.get("scenarioLabel") or "分析报告"),
                 "summary": str(report.get("summaryText") or ""), "created_at": now},
                run_id=run_id, now=now,
            )

    @staticmethod
    def _conversation_report(connection: sqlite3.Connection, conversation_id: str) -> dict[str, Any] | None:
        artifact = connection.execute(
            """SELECT report_id,version,origin_run_id,created_at FROM report_artifacts
               WHERE conversation_id=? ORDER BY version DESC LIMIT 1""",
            (conversation_id,),
        ).fetchone()
        if artifact is not None:
            return {"report_id": str(artifact["report_id"]), "version": int(artifact["version"]),
                    "origin_run_id": str(artifact["origin_run_id"]), "created_at": str(artifact["created_at"])}
        rows = connection.execute(
            "SELECT run_id,result_json,updated_at FROM runs WHERE context_id=? ORDER BY updated_at DESC,run_id DESC",
            (conversation_id,),
        ).fetchall()
        for row in rows:
            result = _load_json(row["result_json"], {}) or {}
            if isinstance(result, dict) and isinstance(result.get("report"), dict):
                return {"report_id": str(row["run_id"]), "version": 1,
                        "origin_run_id": str(row["run_id"]), "created_at": row["updated_at"]}
        return None

    @classmethod
    def _conversation_metadata(cls, connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        latest = connection.execute(
            "SELECT kind,payload_json FROM conversation_entries WHERE conversation_id=? ORDER BY conversation_seq DESC LIMIT 1",
            (str(row["session_id"]),),
        ).fetchone()
        run = connection.execute(
            "SELECT run_id,status,pending_input_json FROM runs WHERE run_id=? AND context_id=?",
            (str(row["active_run_id"] or ""), str(row["session_id"])),
        ).fetchone() if row["active_run_id"] else None
        return {
            "conversation_id": str(row["session_id"]),
            "context_id": str(row["session_id"]),
            "title": str(row["title"] or "新会话"),
            "title_source": str(row["title_source"] or "generated"),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
            "conversation_revision": int(row["conversation_revision"] or 0),
            "active_run_id": str(run["run_id"]) if run else None,
            "active_run_status": str(run["status"]) if run else None,
            "has_pending_input": bool(run and run["pending_input_json"]),
            "selected_report_id": str(row["selected_report_id"]) if row["selected_report_id"] else None,
            "latest_report": cls._conversation_report(connection, str(row["session_id"])),
            "latest_entry_preview": ({"kind": str(latest["kind"]), "payload": _load_json(latest["payload_json"], {})}
                                     if latest else None),
        }

    def create_conversation(self, owner_id: str, command_id: str) -> dict[str, Any]:
        owner = str(owner_id or "").strip()
        command = str(command_id or "").strip()
        if not owner:
            raise ValueError("owner_id_required")
        if not command or len(command) > 128 or any(ord(c) < 32 for c in command):
            raise ValueError("client_request_id_invalid")
        request_hash = canonical_request_hash({"operation": "create_conversation"})
        now = _now()
        with self._transaction() as connection:
            prior = connection.execute(
                "SELECT request_hash,response_json FROM conversation_commands WHERE owner_key=? AND operation='create' AND command_id=?",
                (owner, command),
            ).fetchone()
            if prior:
                if not hmac.compare_digest(str(prior["request_hash"]), request_hash):
                    raise ValueError("command_conflict")
                return _load_json(prior["response_json"], {})
            # token_urlsafe may start with '_' which the shared context ID
            # validator rejects. Prefix IDs so every newly created
            # conversation is valid as a session/context ID.
            conversation_id = f"conv-{secrets.token_urlsafe(24)}"
            connection.execute(
                """INSERT INTO sessions(session_id,owner_id,revision,created_at,updated_at,title,title_source,
                   conversation_revision) VALUES(?,?,0,?,?,?,'generated',0)""",
                (conversation_id, owner, now, now, "新会话"),
            )
            row = self._session_row(connection, conversation_id)
            response = self._conversation_metadata(connection, row)
            connection.execute(
                """INSERT INTO conversation_commands(owner_key,operation,command_id,request_hash,conversation_id,
                   accepted_kind,response_json,created_at) VALUES(?,'create',?,?,?,'created',?,?)""",
                (owner, command, request_hash, conversation_id, _dump(response), now),
            )
            return response

    def get_conversation_owned(self, conversation_id: str, owner_id: str) -> dict[str, Any]:
        value = _validate_session_id(conversation_id)
        connection = self._connect()
        try:
            row = connection.execute("SELECT * FROM sessions WHERE session_id=? AND owner_id=?", (value, str(owner_id))).fetchone()
            if row is None:
                raise KeyError("会话不存在")
            return self._conversation_metadata(connection, row)
        finally:
            connection.close()

    def get_report_owned(self, report_id: str, owner_id: str) -> dict[str, Any]:
        connection = self._connect()
        try:
            row = connection.execute(
                """SELECT a.* FROM report_artifacts a JOIN sessions s ON s.session_id=a.conversation_id
                   WHERE a.report_id=? AND s.owner_id=?""",
                (str(report_id), str(owner_id)),
            ).fetchone()
            if row is not None:
                return {
                    "report_id": str(row["report_id"]), "conversation_id": str(row["conversation_id"]),
                    "origin_run_id": str(row["origin_run_id"]), "version": int(row["version"]),
                    "parent_report_id": row["parent_report_id"],
                    "report_schema_version": int(row["report_schema_version"]),
                    "content": _load_json(row["content_json"], {}), "context_text": str(row["context_text"]),
                    "evidence_manifest": _load_json(row["evidence_manifest_json"], {}),
                    "dataset_version": row["dataset_version"], "created_at": str(row["created_at"]),
                }
            # Preserve run-id report URLs while older rows are gradually
            # represented by immutable artifacts.
            legacy = connection.execute(
                "SELECT r.run_id,r.context_id,r.result_json,r.updated_at FROM runs r WHERE r.run_id=? AND r.owner_id=?",
                (str(report_id), str(owner_id)),
            ).fetchone()
            result = _load_json(legacy["result_json"], {}) if legacy else None
            report = result.get("report") if isinstance(result, dict) else None
            if not isinstance(report, dict):
                raise KeyError("报告不存在")
            return {"report_id": str(legacy["run_id"]), "conversation_id": str(legacy["context_id"]),
                    "origin_run_id": str(legacy["run_id"]), "version": 1, "parent_report_id": None,
                    "report_schema_version": 1, "content": report, "context_text": "",
                    "evidence_manifest": report.get("answerEvidence") or {}, "dataset_version": None,
                    "created_at": str(legacy["updated_at"])}
        finally:
            connection.close()

    def list_conversations_owned(self, owner_id: str, *, cursor: str | None = None, limit: int = 30) -> dict[str, Any]:
        from base64 import urlsafe_b64decode, urlsafe_b64encode
        limit = max(1, min(int(limit), 100))
        after: tuple[str, str] | None = None
        if cursor:
            try:
                decoded = json.loads(urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode("utf-8"))
                after = (str(decoded["updated_at"]), str(decoded["conversation_id"]))
            except Exception as exc:
                raise ValueError("cursor_invalid") from exc
        connection = self._connect()
        try:
            if after:
                rows = connection.execute(
                    """SELECT * FROM sessions WHERE owner_id=? AND (updated_at<? OR (updated_at=? AND session_id>?))
                       ORDER BY updated_at DESC,session_id ASC LIMIT ?""",
                    (str(owner_id), after[0], after[0], after[1], limit + 1),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM sessions WHERE owner_id=? ORDER BY updated_at DESC,session_id ASC LIMIT ?",
                    (str(owner_id), limit + 1),
                ).fetchall()
            has_more = len(rows) > limit
            page = rows[:limit]
            next_cursor = None
            if has_more and page:
                raw = _dump({"updated_at": str(page[-1]["updated_at"]), "conversation_id": str(page[-1]["session_id"])}).encode()
                next_cursor = urlsafe_b64encode(raw).decode().rstrip("=")
            items = []
            for row in page:
                metadata = self._conversation_metadata(connection, row)
                items.append({key: metadata[key] for key in (
                    "conversation_id", "title", "title_source", "created_at", "updated_at",
                    "latest_entry_preview", "active_run_id", "active_run_status", "has_pending_input", "latest_report")})
            return {"items": items, "next_cursor": next_cursor}
        finally:
            connection.close()

    def list_conversation_entries_owned(self, conversation_id: str, owner_id: str, *, before_seq: int | None = None,
                                        limit: int = 50) -> dict[str, Any]:
        value = _validate_session_id(conversation_id)
        limit = max(1, min(int(limit), 100))
        connection = self._connect()
        try:
            owner = connection.execute("SELECT 1 FROM sessions WHERE session_id=? AND owner_id=?", (value, str(owner_id))).fetchone()
            if owner is None:
                raise KeyError("会话不存在")
            high = int(connection.execute("SELECT COALESCE(MAX(conversation_seq),0) FROM conversation_entries WHERE conversation_id=?", (value,)).fetchone()[0])
            if before_seq is None:
                before_seq = high + 1
            rows = connection.execute(
                """SELECT * FROM conversation_entries WHERE conversation_id=? AND conversation_seq<?
                   ORDER BY conversation_seq DESC LIMIT ?""",
                (value, int(before_seq), limit + 1),
            ).fetchall()
            has_more = len(rows) > limit
            page = list(reversed(rows[:limit]))
            entries = [{"entry_id": str(row["entry_id"]), "conversation_id": value,
                        "conversation_seq": int(row["conversation_seq"]), "kind": str(row["kind"]),
                        "run_id": row["run_id"], "turn_id": row["turn_id"], "source_key": str(row["source_key"]),
                        "payload": _load_json(row["payload_json"], {}), "created_at": str(row["created_at"])} for row in page]
            return {"conversation_id": value, "entries": entries, "high_water_seq": high,
                    "next_before_seq": int(page[0]["conversation_seq"]) if has_more and page else None}
        finally:
            connection.close()

    @staticmethod
    def _append_conversation_entry_in_transaction(
        connection: sqlite3.Connection, conversation_id: str, kind: str, source_key: str,
        payload: dict[str, Any], *, run_id: str | None, now: str,
    ) -> str:
        entry_fields = {
            "user_message": {"text", "client_request_id", "delivery_status"},
            "assistant_message": {"text", "format"},
            "execution_group": {"title", "summary", "status", "started_at", "finished_at", "duration_ms", "activities", "plan"},
            "question": {"title", "questions", "tool_call_id", "resume_revision"},
            "question_answer": {"summary", "answer_to_entry_id"},
            "report_card": {"report_id", "version", "origin_run_id", "title", "summary", "created_at"},
            "run_status": {"status", "message", "retryable", "failure_code"},
        }
        if kind not in entry_fields:
            raise ValueError("conversation_entry_invalid")
        safe_payload = {key: value for key, value in payload.items() if key in entry_fields[kind]}
        if kind == "execution_group":
            safe_payload = _safe_execution_group_payload(safe_payload)
        prior = connection.execute(
            "SELECT entry_id FROM conversation_entries WHERE conversation_id=? AND source_key=?",
            (conversation_id, source_key),
        ).fetchone()
        if prior:
            return str(prior["entry_id"])
        seq = int(connection.execute(
            "SELECT COALESCE(MAX(conversation_seq),0)+1 FROM conversation_entries WHERE conversation_id=?",
            (conversation_id,),
        ).fetchone()[0])
        entry_id = secrets.token_urlsafe(18)
        connection.execute(
            """INSERT INTO conversation_entries(entry_id,conversation_id,conversation_seq,kind,run_id,source_key,
               payload_json,created_at) VALUES(?,?,?,?,?,?,?,?)""",
            (entry_id, conversation_id, seq, kind, run_id, source_key, _dump(safe_payload), now),
        )
        connection.execute(
            "UPDATE sessions SET updated_at=?,conversation_revision=conversation_revision+1 WHERE session_id=?",
            (now, conversation_id),
        )
        return entry_id

    def append_conversation_entry(self, conversation_id: str, kind: str, source_key: str,
                                  payload: dict[str, Any], *, run_id: str | None = None,
                                  turn_id: str | None = None) -> dict[str, Any]:
        allowed = {"user_message", "assistant_message", "execution_group", "question", "question_answer", "report_card", "run_status"}
        if kind not in allowed or not source_key or len(source_key) > 256:
            raise ValueError("conversation_entry_invalid")
        fields_by_kind = {
            "user_message": {"text", "client_request_id", "delivery_status"},
            "assistant_message": {"text", "format"},
            "execution_group": {"title", "summary", "status", "started_at", "finished_at", "duration_ms", "activities", "plan"},
            "question": {"title", "questions", "tool_call_id", "resume_revision"},
            "question_answer": {"summary", "answer_to_entry_id"},
            "report_card": {"report_id", "version", "origin_run_id", "title", "summary", "created_at"},
            "run_status": {"status", "message", "retryable", "failure_code"},
        }
        payload = {key: value for key, value in payload.items() if key in fields_by_kind[kind]}
        if kind == "execution_group":
            payload = _safe_execution_group_payload(payload)
        value = _validate_session_id(conversation_id)
        now = _now()
        with self._transaction() as connection:
            row = self._session_row(connection, value)
            if row is None:
                raise KeyError("会话不存在")
            prior = connection.execute("SELECT * FROM conversation_entries WHERE conversation_id=? AND source_key=?", (value, source_key)).fetchone()
            if prior:
                return {"entry_id": prior["entry_id"], "conversation_id": value, "conversation_seq": int(prior["conversation_seq"]),
                        "kind": prior["kind"], "run_id": prior["run_id"], "turn_id": prior["turn_id"],
                        "source_key": prior["source_key"], "payload": _load_json(prior["payload_json"], {}), "created_at": prior["created_at"]}
            seq = int(connection.execute("SELECT COALESCE(MAX(conversation_seq),0)+1 FROM conversation_entries WHERE conversation_id=?", (value,)).fetchone()[0])
            entry_id = secrets.token_urlsafe(18)
            connection.execute(
                "INSERT INTO conversation_entries(entry_id,conversation_id,conversation_seq,kind,run_id,turn_id,source_key,payload_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (entry_id, value, seq, kind, run_id, turn_id, source_key, _dump(payload), now),
            )
            connection.execute("UPDATE sessions SET updated_at=?,conversation_revision=conversation_revision+1 WHERE session_id=?", (now, value))
            return {"entry_id": entry_id, "conversation_id": value, "conversation_seq": seq, "kind": kind,
                    "run_id": run_id, "turn_id": turn_id, "source_key": source_key, "payload": payload, "created_at": now}

    def accept_conversation_message(
        self,
        conversation_id: str,
        *,
        owner_id: str,
        command_id: str,
        content: str,
        source_report_id: str | None = None,
        allow_steering: bool = True,
        admission_limits: dict[str, int] | None = None,
        reserve_tokens: int = 0,
    ) -> dict[str, Any]:
        """Atomically accept one chat command as a new run or active-run steering."""
        context_id = _validate_session_id(conversation_id)
        command = str(command_id or "").strip()
        body = str(content or "").strip()
        if not command or len(command) > 128 or any(ord(char) < 32 for char in command):
            raise ValueError("client_request_id_invalid")
        if not body or len(body) > 20_000:
            raise ValueError("message_invalid")
        source_id = str(source_report_id or "").strip() or None
        request_hash = canonical_request_hash({
            "operation": "conversation_message", "conversation_id": context_id,
            "content": body, "source_report_id": source_id,
        })
        now = _now()
        with self._transaction() as connection:
            prior = connection.execute(
                "SELECT request_hash,response_json FROM conversation_commands WHERE owner_key=? AND operation='message' AND command_id=?",
                (str(owner_id or ""), command),
            ).fetchone()
            if prior is not None:
                if not hmac.compare_digest(str(prior["request_hash"]), request_hash):
                    raise ValueError("command_conflict")
                return _load_json(prior["response_json"], {})

            context = connection.execute(
                "SELECT * FROM sessions WHERE session_id=? AND owner_id=?", (context_id, str(owner_id or "")),
            ).fetchone()
            if context is None:
                raise KeyError("会话不存在")
            if source_id:
                report = connection.execute(
                    "SELECT report_id FROM report_artifacts WHERE report_id=? AND conversation_id=?",
                    (source_id, context_id),
                ).fetchone()
                if report is None:
                    raise ValueError("source_report_not_found")

            active_run_id = str(context["active_run_id"] or "")
            active = connection.execute(
                "SELECT * FROM runs WHERE run_id=? AND owner_id=? AND context_id=?",
                (active_run_id, str(owner_id or ""), context_id),
            ).fetchone() if active_run_id else None
            request_data = {"source_report_id": source_id} if source_id else {}
            accepted_kind = "run"
            run_id: str
            if active is not None:
                active_request = _load_json(active["request_json"], {}) or {}
                active_status = str(active["status"] or "")
                steerable_queued = active_status == "queued" and bool(active_request.get("resume_requested"))
                if active_status in {"running", "waiting_user"} or steerable_queued:
                    if not allow_steering:
                        raise ValueError("midrun_input_unsupported")
                    checkpoint = _load_json(active["checkpoint_json"], {}) or {}
                    finalized = bool((checkpoint.get("flags") or {}).get("answer_finalized"))
                    plan = checkpoint.get("plan") if isinstance(checkpoint.get("plan"), list) else []
                    active_step = next((step for step in plan if isinstance(step, dict) and step.get("status") == "running"), None)
                    if finalized or (active_step and str(active_step.get("kind") or "") == "finalize"):
                        raise ValueError("run_finishing")
                    if source_id:
                        active_request = {**active_request, "source_report_id": source_id}
                        connection.execute(
                            "UPDATE runs SET source_report_id=?,request_json=? WHERE run_id=?",
                            (source_id, _dump(active_request), active_run_id),
                        )
                    accepted = self._accept_run_message_in_transaction(
                        connection, active_run_id, owner_id=str(owner_id), client_message_id=command,
                        content=body, expected_revision=int(((_load_json(active["runtime_json"], {}) or {}).get("input_revision") or 0)),
                        allow_queued_resume=True, source_report_id=source_id,
                    )
                    if accepted is None:
                        raise ValueError("conversation_run_conflict")
                    run_id = active_run_id
                    accepted_kind = "steering"
                else:
                    connection.execute(
                        "UPDATE sessions SET active_run_id=NULL WHERE session_id=? AND active_run_id=?",
                        (context_id, active_run_id),
                    )
                    active = None

            if active is None:
                run_id = f"run-{secrets.token_hex(20)}"
                session_id = f"conversation-{secrets.token_hex(16)}"
                self._check_admission(connection, str(owner_id), admission_limits or {}, int(reserve_tokens))
                if source_id:
                    request_data = {"source_report_id": source_id}
                connection.execute(
                    """INSERT INTO runs(run_id,owner_id,session_id,context_id,input_text,context_revision,request_json,status,
                       next_seq,resume_revision,created_at,updated_at,queued_at,source_report_id)
                       VALUES(?,?,?,?,?,?,?,'queued',1,0,?,?,?,?)""",
                    (run_id, str(owner_id), session_id, context_id, body, int(context["revision"] or 0),
                     _dump(request_data), now, now, now, source_id),
                )
                connection.execute(
                    "INSERT INTO admission_usage(run_id,owner_id,usage_day,reserved_tokens) VALUES(?,?,?,?)",
                    (run_id, str(owner_id), datetime.now(UTC).date().isoformat(), max(0, int(reserve_tokens))),
                )

            entry_id = self._append_conversation_entry_in_transaction(
                connection, context_id, "user_message", f"message:{command}",
                {"text": body, "client_request_id": command, "delivery_status": "accepted"},
                run_id=run_id, now=now,
            )
            entry_row = connection.execute(
                "SELECT * FROM conversation_entries WHERE entry_id=?", (entry_id,),
            ).fetchone()
            entry = {
                "entry_id": entry_id, "conversation_id": context_id,
                "conversation_seq": int(entry_row["conversation_seq"]), "kind": str(entry_row["kind"]),
                "run_id": entry_row["run_id"], "turn_id": entry_row["turn_id"],
                "source_key": str(entry_row["source_key"]),
                "payload": _load_json(entry_row["payload_json"], {}), "created_at": str(entry_row["created_at"]),
            }
            response = {
                "conversation_id": context_id, "accepted_kind": accepted_kind, "run_id": run_id,
                "client_request_id": command, "entry": entry,
                "status": accepted["status"] if accepted_kind == "steering" else "queued",
                "input_revision": accepted["revision"] if accepted_kind == "steering" else None,
            }
            connection.execute(
                """INSERT INTO conversation_commands(owner_key,operation,command_id,request_hash,conversation_id,
                   accepted_kind,run_id,response_json,created_at) VALUES(?,'message',?,?,?,?,?,?,?)""",
                (str(owner_id), command, request_hash, context_id, accepted_kind, run_id, _dump(response), now),
            )
            return response

    def _events(self, connection: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
        rows = connection.execute(
            "SELECT payload_json FROM run_events WHERE run_id=? ORDER BY seq",
            (run_id,),
        ).fetchall()
        return [_load_json(row["payload_json"], {}) for row in rows]

    def _execution_group_payload(
        self, connection: sqlite3.Connection, run_id: str, snapshot: dict[str, Any],
        *, status: str, finished_at: str,
    ) -> dict[str, Any] | None:
        """Build a durable, redacted summary from the run's public event projection."""
        try:
            raw_events = self._events(connection, run_id)
        except RuntimeError:
            # Execution summaries are supplemental. A malformed historical
            # event must not prevent the validated answer from committing.
            raw_events = []
        raw_events = [event for event in raw_events if isinstance(event, dict)]
        projected_events = list(raw_events)
        runtime = snapshot.get("runtime") if isinstance(snapshot.get("runtime"), dict) else {}
        if not any(event.get("event_type") == "plan_update" for event in raw_events) and isinstance(runtime.get("plan"), list):
            projected_events.append({"event_type": "plan_update", "output": {"steps": runtime["plan"]}})
        has_tool_events = any(event.get("event_type") in {"tool_started", "tool_completed", "tool_call", "tool_rejected", "input_required"}
                              for event in raw_events)
        if not has_tool_events and isinstance(runtime.get("activities"), list):
            for raw_activity in runtime["activities"]:
                if not isinstance(raw_activity, dict):
                    continue
                activity_status = raw_activity.get("status")
                event_type = {"running": "tool_started", "waiting": "input_required", "error": "tool_rejected"}.get(
                    activity_status, "tool_completed",
                )
                activity_output = dict(raw_activity.get("output")) if isinstance(raw_activity.get("output"), dict) else {}
                detail = raw_activity.get("detail")
                if isinstance(detail, str) and not any(key in activity_output for key in ("note", "error", "error_code")):
                    activity_output["error" if activity_status == "error" else "note"] = detail
                projected_events.append({
                    "event_type": event_type,
                    "input": {
                        "tool_call_id": raw_activity.get("id"),
                        "tool_name": raw_activity.get("toolName") or raw_activity.get("tool_name"),
                        "plan_step_id": raw_activity.get("planStepId") or raw_activity.get("plan_step_id"),
                    },
                    "output": activity_output,
                    "status": "error" if activity_status == "error" else str(activity_status or "done"),
                    "duration_ms": raw_activity.get("duration_ms"),
                })
        activities: dict[str, dict[str, Any]] = {}
        plan: list[dict[str, Any]] = []
        duration_ms = 0
        for index, event in enumerate(projected_events):
            projected = public_run_event(event)
            event_type = projected.get("event_type")
            if event_type == "plan_update":
                steps = (projected.get("output") or {}).get("steps")
                if isinstance(steps, list):
                    plan = steps
                continue
            if event_type not in {"tool_started", "tool_completed", "tool_call", "tool_rejected", "input_required"}:
                continue
            event_input = projected.get("input") if isinstance(projected.get("input"), dict) else {}
            tool_name = event_input.get("tool_name")
            if not isinstance(tool_name, str) or not tool_name:
                continue
            call_id = event_input.get("tool_call_id")
            activity_id = str(call_id or f"{run_id}:tool:{index}")
            activity = activities.setdefault(activity_id, {
                "id": activity_id, "tool_name": tool_name, "status": "running",
            })
            activity["tool_name"] = tool_name
            plan_step_id = event_input.get("plan_step_id")
            if isinstance(plan_step_id, str) and plan_step_id:
                activity["plan_step_id"] = plan_step_id
            if event_type == "tool_started":
                activity["status"] = "running"
                activity["detail"] = "正在执行"
            elif event_type == "input_required":
                activity["status"] = "waiting"
                activity["detail"] = "等待你的回答"
            else:
                output = projected.get("output") if isinstance(projected.get("output"), dict) else {}
                event_status = str(projected.get("status") or "").lower()
                failed = event_type == "tool_rejected" or output.get("ok") is False or event_status in {"error", "failed", "rejected"}
                activity["status"] = "error" if failed else "complete"
                detail = output.get("error") or output.get("note") or output.get("error_code")
                activity["detail"] = str(detail or ("调用未通过" if failed else "已完成"))[:500]
                if output:
                    activity["output"] = output
                if event_type == "tool_completed":
                    event_duration = projected.get("duration_ms")
                    if isinstance(event_duration, int):
                        duration_ms += event_duration
        if not projected_events and not plan and not activities:
            return None

        terminal_status = "complete" if status == "done" else "error"
        summary = (f"本轮执行完成 · {len(plan)} 项计划 · {len(activities)} 项工具活动。"
                   if terminal_status == "complete" else "本轮未能完成；展开查看已发生的步骤。")
        return _safe_execution_group_payload({
            "title": "本轮执行",
            "summary": summary,
            "status": terminal_status,
            "started_at": snapshot.get("started_at") or snapshot.get("created_at"),
            "finished_at": finished_at,
            "duration_ms": duration_ms,
            "activities": list(activities.values())[-50:],
            "plan": plan,
        })

    def _run_snapshot(self, connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        snapshot = {
            "run_id": row["run_id"],
            "owner_id": row["owner_id"],
            "session_id": row["session_id"],
            "context_id": row["context_id"],
            "context_revision": int(row["context_revision"] or 0),
            "state_revision": int(row["state_revision"] or 0),
            "input": row["input_text"],
            "request": _load_json(row["request_json"], {}),
            "status": row["status"],
            "next_seq": row["next_seq"],
            "covered_event_seq": int(row["covered_event_seq"] or 0),
            "resume_revision": row["resume_revision"],
            "result": _load_json(row["result_json"]),
            "error": row["error"],
            "checkpoint": _load_json(row["checkpoint_json"]),
            "pending_input": _load_json(row["pending_input_json"]),
            "runtime": _load_json(row["runtime_json"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "queued_at": row["queued_at"],
            "started_at": row["started_at"],
            "heartbeat_at": row["heartbeat_at"],
            "finished_at": row["finished_at"],
            "attempt": row["attempt"],
            "worker_id": row["worker_id"],
            "lease_token_hash": row["lease_token_hash"],
            "lease_expires_at": row["lease_expires_at"],
            "failure_code": row["failure_code"],
            "retryable": bool(row["retryable"]),
            "cancel_requested_at": row["cancel_requested_at"],
            "version": row["version"],
        }
        return snapshot

    def _get_run(self, connection: sqlite3.Connection, run_id: str) -> dict[str, Any]:
        row = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"运行不存在：{run_id}")
        return self._run_snapshot(connection, row)

    def create_run(
        self,
        run_id: str,
        session_id: str,
        input_text: str,
        *,
        context_id: str | None = None,
        owner_id: str | None = None,
        request_data: dict[str, Any] | None = None,
        client_request_id: str | None = None,
        idempotency_operation: str = "submit",
        admission_limits: dict[str, int] | None = None,
        reserve_tokens: int = 0,
    ) -> dict[str, Any]:
        value = _validate_run_id(run_id)
        session = _validate_session_id(session_id)
        context = _validate_session_id(context_id or session)
        request_id = str(client_request_id or "").strip()
        if len(request_id) > 128 or any(ord(char) < 32 for char in request_id):
            raise ValueError("client_request_id 格式无效")
        request_hash = canonical_request_hash({
            "operation": idempotency_operation,
            "session_id": session,
            "context_id": context,
            "input": str(input_text),
            "request": request_data or {},
        })
        now = _now()
        with self._transaction() as connection:
            if request_id:
                request = connection.execute(
                    "SELECT request_hash,run_id FROM client_requests WHERE owner_key=? AND operation=? AND client_request_id=?",
                    (str(owner_id or ""), str(idempotency_operation), request_id),
                ).fetchone()
                if request is not None:
                    if not hmac.compare_digest(str(request["request_hash"]), request_hash):
                        raise ValueError("client_request_conflict")
                    return self._get_run(connection, str(request["run_id"]))
            existing = connection.execute("SELECT owner_id FROM runs WHERE run_id=?", (value,)).fetchone()
            if existing is not None:
                if owner_id is not None and str(existing["owner_id"] or "") != str(owner_id):
                    raise KeyError("运行不存在")
                snapshot = self._get_run(connection, value)
                if (
                    str(snapshot.get("session_id") or "") != session
                    or str(snapshot.get("context_id") or "") != context
                    or str(snapshot.get("input") or "") != str(input_text)
                    or (snapshot.get("request") or {}) != (request_data or {})
                ):
                    raise ValueError("run_id_conflict")
                if request_id:
                    connection.execute(
                        "INSERT INTO client_requests(owner_key,operation,client_request_id,request_hash,run_id,created_at) VALUES(?,?,?,?,?,?)",
                        (str(owner_id or ""), str(idempotency_operation), request_id, request_hash, value, now),
                    )
                return snapshot
            self._check_admission(connection, str(owner_id or ""), admission_limits or {}, int(reserve_tokens))
            context_row = self._session_row(connection, context)
            if context_row is None:
                connection.execute(
                    "INSERT INTO sessions(session_id,owner_id,revision,created_at,updated_at) VALUES(?,?,0,?,?)",
                    (context, str(owner_id) if owner_id is not None else None, now, now),
                )
                context_revision = 0
            else:
                if owner_id is not None and (
                    context_row["owner_id"] is None or str(context_row["owner_id"]) != str(owner_id)
                ):
                    raise KeyError("会话不存在")
                context_revision = int(context_row["revision"] or 0)
            connection.execute(
                """INSERT INTO runs(run_id,owner_id,session_id,context_id,input_text,context_revision,request_json,status,next_seq,
                   resume_revision,created_at,updated_at,queued_at) VALUES(?,?,?,?,?,?,?,'queued',1,0,?,?,?)""",
                (value, str(owner_id) if owner_id is not None else None, session, context, str(input_text), context_revision,
                 _dump(request_data or {}), now, now, now),
            )
            if request_id:
                connection.execute(
                    "INSERT INTO client_requests(owner_key,operation,client_request_id,request_hash,run_id,created_at) VALUES(?,?,?,?,?,?)",
                    (str(owner_id or ""), str(idempotency_operation), request_id, request_hash, value, now),
                )
            connection.execute(
                "INSERT INTO admission_usage(run_id,owner_id,usage_day,reserved_tokens) VALUES(?,?,?,?)",
                (value, str(owner_id or ""), datetime.now(UTC).date().isoformat(), max(0, int(reserve_tokens))),
            )
            return self._get_run(connection, value)

    @staticmethod
    def _check_admission(connection: sqlite3.Connection, owner_id: str,
                         limits: dict[str, int], reserve_tokens: int) -> None:
        global_limit = max(0, int(limits.get("global_pending") or 0))
        owner_limit = max(0, int(limits.get("owner_pending") or 0))
        daily_limit = max(0, int(limits.get("daily_tokens") or 0))
        pending = "status IN ('queued','running','waiting_user')"
        if global_limit:
            count = int(connection.execute(f"SELECT COUNT(*) FROM runs WHERE {pending}").fetchone()[0])
            if count >= global_limit:
                raise ValueError("admission_global_pending_limit")
        if owner_limit:
            count = int(connection.execute(f"SELECT COUNT(*) FROM runs WHERE owner_id=? AND {pending}", (owner_id,)).fetchone()[0])
            if count >= owner_limit:
                raise ValueError("admission_owner_pending_limit")
        if daily_limit:
            today = datetime.now(UTC).date().isoformat()
            row = connection.execute(
                "SELECT COALESCE(SUM(reserved_tokens+actual_tokens),0) FROM admission_usage WHERE usage_day=?",
                (today,),
            ).fetchone()
            if int(row[0]) + max(0, reserve_tokens) > daily_limit:
                raise ValueError("admission_daily_token_limit")

    @staticmethod
    def _reserve_existing_admission(connection: sqlite3.Connection, run_id: str, owner_id: str,
                                    limits: dict[str, int], reserve_tokens: int) -> None:
        daily_limit = max(0, int(limits.get("daily_tokens") or 0))
        existing = connection.execute(
            "SELECT usage_day FROM admission_usage WHERE run_id=?", (run_id,),
        ).fetchone()
        usage_day = str(existing["usage_day"]) if existing is not None else datetime.now(UTC).date().isoformat()
        if daily_limit:
            row = connection.execute(
                "SELECT COALESCE(SUM(reserved_tokens+actual_tokens),0) FROM admission_usage WHERE usage_day=?",
                (usage_day,),
            ).fetchone()
            if int(row[0]) + max(0, reserve_tokens) > daily_limit:
                raise ValueError("admission_daily_token_limit")
        # Recovery cannot prove what the lost worker spent after its last
        # checkpoint. Charge that segment's outstanding reservation before
        # reserving the remaining calls; never refund unknown provider work.
        cursor = connection.execute(
            "UPDATE admission_usage SET actual_tokens=actual_tokens+reserved_tokens,reserved_tokens=? WHERE run_id=?",
            (max(0, int(reserve_tokens)), run_id),
        )
        if cursor.rowcount == 0:
            connection.execute(
                "INSERT INTO admission_usage(run_id,owner_id,usage_day,reserved_tokens) VALUES(?,?,?,?)",
                (run_id, owner_id, datetime.now(UTC).date().isoformat(), max(0, int(reserve_tokens))),
            )

    @staticmethod
    def _settle_admission_usage(connection: sqlite3.Connection, run_id: str, result: dict[str, Any] | None) -> None:
        state = result.get("state") if isinstance(result, dict) else None
        budget = state.get("run_budget") if isinstance(state, dict) else None
        if not isinstance(budget, dict):
            row = connection.execute("SELECT checkpoint_json FROM runs WHERE run_id=?", (run_id,)).fetchone()
            checkpoint = _load_json(row["checkpoint_json"], {}) if row is not None else {}
            budget = checkpoint.get("run_budget") if isinstance(checkpoint, dict) else None
        if isinstance(budget, dict):
            actual = max(0, int(budget.get("total_tokens") or 0))
        else:
            row = connection.execute("SELECT reserved_tokens FROM admission_usage WHERE run_id=?", (run_id,)).fetchone()
            actual = max(0, int(row["reserved_tokens"] or 0)) if row is not None else 0
        connection.execute(
            "UPDATE admission_usage SET reserved_tokens=0,actual_tokens=actual_tokens+? WHERE run_id=?",
            (actual, run_id),
        )

    def get_run(self, run_id: str) -> dict[str, Any]:
        value = _validate_run_id(run_id)
        connection = self._connect()
        try:
            return self._get_run(connection, value)
        finally:
            connection.close()

    def get_run_owned(self, run_id: str, owner_id: str) -> dict[str, Any]:
        snapshot = self.get_run(run_id)
        if not owner_id or str(snapshot.get("owner_id") or "") != str(owner_id):
            raise KeyError("运行不存在")
        return snapshot

    def load_run_owned(self, run_id: str, owner_id: str) -> dict[str, Any]:
        return self.get_run_owned(run_id, owner_id)

    def start_run(self, run_id: str) -> dict[str, Any]:
        value = _validate_run_id(run_id)
        now = _now()
        with self._transaction() as connection:
            connection.execute(
                "UPDATE runs SET status='running',started_at=COALESCE(started_at,?),updated_at=?,state_revision=state_revision+1,version=version+1 WHERE run_id=? AND status NOT IN ('done','error','cancelled')",
                (now, now, value),
            )
            return self._get_run(connection, value)

    def claim_start(self, run_id: str) -> bool:
        value = _validate_run_id(run_id)
        now = _now()
        with self._transaction() as connection:
            cursor = connection.execute(
                "UPDATE runs SET status='running',started_at=COALESCE(started_at,?),heartbeat_at=?,updated_at=?,state_revision=state_revision+1,version=version+1 WHERE run_id=? AND status='queued'",
                (now, now, now, value),
            )
            return cursor.rowcount == 1

    def update_run_request(self, run_id: str, request_data: dict[str, Any]) -> dict[str, Any]:
        value = _validate_run_id(run_id)
        if not isinstance(request_data, dict):
            raise ValueError("request_data 必须是对象")
        now = _now()
        with self._transaction() as connection:
            connection.execute(
                "UPDATE runs SET request_json=?,updated_at=?,state_revision=state_revision+1,version=version+1 WHERE run_id=?",
                (_dump(request_data), now, value),
            )
            return self._get_run(connection, value)

    def prepare_run_resume(
        self,
        run_id: str,
        request_data: dict[str, Any],
        *,
        expected_tool_call_id: str | None = None,
        expected_revision: int | None = None,
        client_request_id: str | None = None,
        request_hash: str | None = None,
    ) -> bool:
        value = _validate_run_id(run_id)
        if not isinstance(request_data, dict):
            raise ValueError("request_data 必须是对象")
        now = _now()
        request_id = str(client_request_id or "").strip()
        if len(request_id) > 128 or any(ord(char) < 32 for char in request_id):
            raise ValueError("client_request_id 格式无效")
        with self._transaction() as connection:
            snapshot = self._get_run(connection, value)
            request_key = str(snapshot.get("owner_id") or "")
            if request_id:
                existing_command = connection.execute(
                    "SELECT request_hash FROM client_requests WHERE owner_key=? AND operation='input' AND client_request_id=?",
                    (request_key, request_id),
                ).fetchone()
                if existing_command is not None:
                    if not hmac.compare_digest(str(existing_command["request_hash"]), str(request_hash or "")):
                        raise ValueError("client_request_conflict")
                    return str(connection.execute(
                        "SELECT run_id FROM client_requests WHERE owner_key=? AND operation='input' AND client_request_id=?",
                        (request_key, request_id),
                    ).fetchone()["run_id"]) == value
            if snapshot.get("status") != "waiting_user":
                return False
            if (snapshot.get("request") or {}).get("resume_ready"):
                return False
            pending = snapshot.get("pending_input") or {}
            if expected_tool_call_id is not None and str(pending.get("tool_call_id") or "") != str(expected_tool_call_id):
                return False
            revision = int(snapshot.get("resume_revision") or 0)
            if expected_revision is not None and revision != int(expected_revision):
                return False
            next_revision = revision + 1
            request_data = {
                **request_data,
                "resume_ready": True,
                "accepted_tool_call_id": str(pending.get("tool_call_id") or ""),
                "accepted_resume_revision": next_revision,
            }
            cursor = connection.execute(
                "UPDATE runs SET request_json=?,updated_at=?,state_revision=state_revision+1,version=version+1 WHERE run_id=? AND status='waiting_user' AND resume_revision=?",
                (_dump(request_data), now, value, revision),
            )
            if cursor.rowcount != 1:
                return False
            if request_id:
                connection.execute(
                    "INSERT INTO client_requests(owner_key,operation,client_request_id,request_hash,run_id,response_json,created_at) VALUES(?,'input',?,?,?,?,?)",
                    (request_key, request_id, str(request_hash or ""), value,
                     _dump({"accepted_tool_call_id": request_data["accepted_tool_call_id"],
                            "resume_revision": next_revision}), now),
                )
            return True

    def get_client_request(self, owner_id: str, operation: str, client_request_id: str,
                           request_hash: str) -> dict[str, Any] | None:
        request_key = str(owner_id or "")
        request_id = str(client_request_id or "").strip()
        if not request_id:
            return None
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT request_hash,run_id,response_json FROM client_requests WHERE owner_key=? AND operation=? AND client_request_id=?",
                (request_key, str(operation), request_id),
            ).fetchone()
            if row is None:
                return None
            if not hmac.compare_digest(str(row["request_hash"]), str(request_hash or "")):
                raise ValueError("client_request_conflict")
            return {"run_id": str(row["run_id"]), "response": _load_json(row["response_json"], {})}
        finally:
            connection.close()

    def peek_client_request(self, owner_id: str, operation: str, client_request_id: str) -> dict[str, Any] | None:
        request_id = str(client_request_id or "").strip()
        if not request_id:
            return None
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT request_hash,run_id,response_json FROM client_requests WHERE owner_key=? AND operation=? AND client_request_id=?",
                (str(owner_id or ""), str(operation), request_id),
            ).fetchone()
            if row is None:
                return None
            return {"request_hash": str(row["request_hash"]), "run_id": str(row["run_id"]),
                    "response": _load_json(row["response_json"], {})}
        finally:
            connection.close()

    @staticmethod
    def _lease_token_hash(token: str) -> str:
        return hashlib.sha256(str(token).encode("utf-8")).hexdigest()

    @staticmethod
    def _time_value(value: str) -> datetime:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)

    def _lease_is_valid(self, row: sqlite3.Row, lease_token: str | None, now: str | None = None) -> bool:
        if row["status"] in _TERMINAL:
            return False
        stored_hash = row["lease_token_hash"]
        # Keep explicit legacy/test stores usable; a persisted lease always requires its fencing token.
        if not stored_hash:
            return lease_token is None
        if not lease_token or not hmac.compare_digest(str(stored_hash), self._lease_token_hash(lease_token)):
            return False
        if row["status"] != "running" or not row["lease_expires_at"]:
            return False
        return self._time_value(row["lease_expires_at"]) > self._time_value(now or _now())

    def acquire_run_lease(
        self, run_id: str, worker_id: str, *, lease_ttl_s: float = 30.0
    ) -> dict[str, Any] | None:
        value = _validate_run_id(run_id)
        if lease_ttl_s <= 0:
            raise ValueError("lease_ttl_s 必须为正数")
        now_dt = datetime.now(UTC)
        now = now_dt.isoformat()
        expires = datetime.fromtimestamp(now_dt.timestamp() + float(lease_ttl_s), UTC).isoformat()
        token = secrets.token_urlsafe(32)
        with self._transaction() as connection:
            row = connection.execute("SELECT * FROM runs WHERE run_id=?", (value,)).fetchone()
            if row is None:
                raise KeyError(f"运行不存在：{value}")
            status = str(row["status"])
            request = _load_json(row["request_json"], {}) or {}
            resume_ready = status == "waiting_user" and bool(request.get("resume_ready"))
            if status != "queued" and not resume_ready:
                return None
            context = self._session_row(connection, str(row["context_id"] or ""))
            if context is None or str(context["owner_id"] or "") != str(row["owner_id"] or ""):
                return None
            active_run_id = str(context["active_run_id"] or "")
            if active_run_id and active_run_id != value:
                return None
            earlier = connection.execute(
                """SELECT run_id FROM runs WHERE context_id=? AND run_id<>?
                   AND (status='queued' OR (status='waiting_user' AND json_extract(request_json,'$.resume_ready')=1))
                   AND (created_at<? OR (created_at=? AND run_id<?))
                   ORDER BY created_at,run_id LIMIT 1""",
                (str(row["context_id"]), value, str(row["created_at"]), str(row["created_at"]), value),
            ).fetchone()
            if earlier is not None:
                return None
            context_revision = int(context["revision"] or 0)
            if request.get("resume_requested") and context_revision != int(row["context_revision"] or 0):
                return None
            queue_wait_ms = max(
                0,
                int((now_dt - self._time_value(str(row["updated_at"] or row["created_at"]))).total_seconds() * 1000),
            )
            connection.execute(
                "UPDATE sessions SET active_run_id=? WHERE session_id=? AND (active_run_id IS NULL OR active_run_id=?)",
                (value, str(row["context_id"]), value),
            )
            revision = int(row["resume_revision"] or 0) + (1 if resume_ready else 0)
            runtime = _load_json(row["runtime_json"], {}) or {}
            if isinstance(runtime, dict):
                runtime = {**runtime, "status": "running"}
            attempt = int(row["attempt"] or 0) + 1
            connection.execute(
                """UPDATE runs SET status='running',resume_revision=?,context_revision=?,state_revision=state_revision+1,runtime_json=?,started_at=COALESCE(started_at,?),
                   heartbeat_at=?,updated_at=?,attempt=?,worker_id=?,lease_token_hash=?,lease_expires_at=?,
                   failure_code=NULL,retryable=0,cancel_requested_at=NULL,version=version+1
                   WHERE run_id=? AND status=?""",
                (revision, context_revision, _dump(runtime), now, now, now, attempt, str(worker_id), self._lease_token_hash(token),
                 expires, value, status),
            )
            if resume_ready:
                request = {**request, "resume_ready": False}
                connection.execute("UPDATE runs SET request_json=? WHERE run_id=?", (_dump(request), value))
            return {
                "run_id": value,
                "worker_id": str(worker_id),
                "token": token,
                "expires_at": expires,
                "attempt": attempt,
                "queue_wait_ms": queue_wait_ms,
            }

    def heartbeat_run(self, run_id: str, lease_token: str, *, lease_ttl_s: float = 30.0) -> bool:
        value = _validate_run_id(run_id)
        if lease_ttl_s <= 0:
            raise ValueError("lease_ttl_s 必须为正数")
        now_dt = datetime.now(UTC)
        now = now_dt.isoformat()
        expires = datetime.fromtimestamp(now_dt.timestamp() + float(lease_ttl_s), UTC).isoformat()
        with self._transaction() as connection:
            row = connection.execute("SELECT * FROM runs WHERE run_id=?", (value,)).fetchone()
            if row is None:
                return False
            if not self._lease_is_valid(row, lease_token, now):
                return False
            connection.execute(
                "UPDATE runs SET heartbeat_at=?,lease_expires_at=?,updated_at=?,version=version+1 WHERE run_id=?",
                (now, expires, now, value),
            )
            return True

    def claim_waiting(
        self,
        run_id: str,
        *,
        expected_tool_call_id: str | None = None,
        expected_revision: int | None = None,
    ) -> bool:
        value = _validate_run_id(run_id)
        now = _now()
        with self._transaction() as connection:
            snapshot = self._get_run(connection, value)
            if snapshot.get("status") != "waiting_user":
                return False
            pending = snapshot.get("pending_input") or {}
            if expected_tool_call_id is not None and str(pending.get("tool_call_id") or "") != str(expected_tool_call_id):
                return False
            revision = int(snapshot.get("resume_revision") or 0)
            if expected_revision is not None and revision != int(expected_revision):
                return False
            runtime = snapshot.get("runtime") or {}
            if isinstance(runtime, dict):
                runtime = {**runtime, "status": "running"}
            cursor = connection.execute(
                "UPDATE runs SET status='running',resume_revision=?,runtime_json=?,updated_at=?,state_revision=state_revision+1,version=version+1 WHERE run_id=? AND status='waiting_user' AND resume_revision=?",
                (revision + 1, _dump(runtime), now, value, revision),
            )
            return cursor.rowcount == 1

    def pause_run(
        self,
        run_id: str,
        checkpoint: dict[str, Any],
        pending_input: dict[str, Any],
        runtime: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
        *,
        waiting_event: dict[str, Any] | None = None,
        session_messages: list[dict[str, Any]] | None = None,
        session_run_id: str | None = None,
        lease_token: str | None = None,
    ) -> dict[str, Any] | None:
        value = _validate_run_id(run_id)
        validate_pending_input(pending_input)
        if not isinstance(checkpoint, dict):
            raise ValueError("waiting checkpoint 必须是对象")
        if waiting_event is not None and not isinstance(waiting_event, dict):
            raise ValueError("waiting_event 必须是对象")
        if session_messages is not None and not isinstance(session_messages, list):
            raise ValueError("session_messages 必须是列表")
        messages = list(session_messages or [])
        if messages and not str(session_run_id or "").strip():
            raise ValueError("session_run_id is required when committing session messages")
        now = _now()
        with self._transaction() as connection:
            snapshot = self._get_run(connection, value)
            row = connection.execute("SELECT * FROM runs WHERE run_id=?", (value,)).fetchone()
            if snapshot["status"] in _TERMINAL:
                return snapshot
            if snapshot["status"] == "waiting_user" and messages and connection.execute(
                "SELECT 1 FROM session_messages WHERE session_id=? AND run_id=? LIMIT 1",
                (str(row["context_id"] or ""), str(session_run_id)),
            ).fetchone():
                return snapshot
            if not self._lease_is_valid(row, lease_token, now):
                return None
            next_context_revision = int(row["context_revision"] or 0)
            if session_messages is not None:
                next_context_revision = self._commit_session_delta(
                    connection, row, messages, str(session_run_id or ""), now,
                )
            next_state_revision = int(snapshot.get("state_revision") or 0) + 1
            capabilities = runtime.get("capabilities") if isinstance(runtime, dict) else None
            runtime_projection = runtime_from_checkpoint(
                checkpoint,
                status="waiting_user",
                state_revision=next_state_revision,
                capabilities=capabilities if isinstance(capabilities, dict) else None,
            )
            connection.execute(
                """UPDATE runs SET status='waiting_user',checkpoint_json=?,pending_input_json=?,runtime_json=?,state_revision=state_revision+1,
                   result_json=COALESCE(?,result_json),context_revision=?,covered_event_seq=next_seq-1,
                   lease_token_hash=NULL,lease_expires_at=NULL,worker_id=NULL,updated_at=?,version=version+1 WHERE run_id=?""",
                (
                    _dump(_redact(checkpoint)),
                    _dump(_redact(pending_input)),
                    _dump(_redact(runtime_projection)),
                    _dump(_redact(result)) if result is not None else None,
                    next_context_revision,
                    now,
                    value,
                ),
            )
            if waiting_event is not None:
                waiting_record = self._append_event_in_transaction(
                    connection,
                    value,
                    {**waiting_event, "pending_input": pending_input,
                     "resume_revision": int(snapshot.get("resume_revision") or 0)},
                    touch_run=False,
                )
                connection.execute(
                    "UPDATE runs SET next_seq=?,covered_event_seq=? WHERE run_id=?",
                    (int(waiting_record["seq"]) + 1, int(waiting_record["seq"]), value),
                )
            return self._get_run(connection, value)

    def save_run_checkpoint(
        self, run_id: str, checkpoint: dict[str, Any], *, lease_token: str | None = None
    ) -> bool:
        """Persist a resumable safe-boundary checkpoint under the active lease."""
        value = _validate_run_id(run_id)
        now = _now()
        with self._transaction() as connection:
            row = connection.execute("SELECT * FROM runs WHERE run_id=?", (value,)).fetchone()
            if row is None or not self._lease_is_valid(row, lease_token, now):
                return False
            next_state_revision = int(row["state_revision"] or 0) + 1
            runtime = runtime_from_checkpoint(
                checkpoint,
                status="running",
                state_revision=next_state_revision,
            )
            cursor = connection.execute(
                "UPDATE runs SET checkpoint_json=?,runtime_json=?,covered_event_seq=next_seq-1,updated_at=?,state_revision=state_revision+1,version=version+1 WHERE run_id=? AND status='running'",
                (_dump(_redact(checkpoint)), _dump(_redact(runtime)), now, value),
            )
            return cursor.rowcount == 1

    def recover_run(self, run_id: str, *, owner_id: str, expected_version: int,
                    client_request_id: str | None = None, request_hash: str | None = None,
                    admission_limits: dict[str, int] | None = None, reserve_tokens: int = 0) -> dict[str, Any] | None:
        """CAS a recoverable worker_lost run back to queued without changing its logical identity."""
        value = _validate_run_id(run_id)
        now = _now()
        request_id = str(client_request_id or "").strip()
        if len(request_id) > 128 or any(ord(char) < 32 for char in request_id):
            raise ValueError("client_request_id 格式无效")
        with self._transaction() as connection:
            if request_id:
                existing_command = connection.execute(
                    "SELECT request_hash,run_id FROM client_requests WHERE owner_key=? AND operation='recover' AND client_request_id=?",
                    (str(owner_id or ""), request_id),
                ).fetchone()
                if existing_command is not None:
                    if not hmac.compare_digest(str(existing_command["request_hash"]), str(request_hash or "")):
                        raise ValueError("client_request_conflict")
                    if str(existing_command["run_id"]) != value:
                        return None
                    return self._get_run(connection, value)
            row = connection.execute("SELECT * FROM runs WHERE run_id=?", (value,)).fetchone()
            if row is None or str(row["owner_id"] or "") != str(owner_id or ""):
                return None
            snapshot = self._run_snapshot(connection, row)
            checkpoint = snapshot.get("checkpoint")
            if (snapshot.get("status") != "error" or snapshot.get("failure_code") != "worker_lost"
                    or not isinstance(checkpoint, dict) or snapshot.get("pending_input")
                    or int(snapshot.get("version") or 0) != int(expected_version)):
                return None
            context = self._session_row(connection, str(row["context_id"] or ""))
            if context is None or int(context["revision"] or 0) != int(row["context_revision"] or 0):
                return None
            self._check_admission(connection, str(owner_id), {**(admission_limits or {}), "daily_tokens": 0}, 0)
            self._reserve_existing_admission(connection, value, str(owner_id), admission_limits or {}, int(reserve_tokens))
            request = {**(snapshot.get("request") or {}), "resume_requested": True,
                       "input_answers": [], "resume_ready": False}
            cursor = connection.execute(
                """UPDATE runs SET status='queued',request_json=?,error=NULL,failure_code=NULL,retryable=0,state_revision=state_revision+1,
                   finished_at=NULL,queued_at=?,updated_at=?,version=version+1 WHERE run_id=? AND status='error'
                   AND failure_code='worker_lost' AND version=?""",
                (_dump(request), now, now, value, int(expected_version)),
            )
            if cursor.rowcount != 1:
                return None
            self._append_event_in_transaction(connection, value,
                {"event_type": "recovery_accepted", "status": "queued"}, touch_run=False)
            connection.execute("UPDATE runs SET next_seq=next_seq+1 WHERE run_id=?", (value,))
            if request_id:
                connection.execute(
                    "INSERT INTO client_requests(owner_key,operation,client_request_id,request_hash,run_id,response_json,created_at) VALUES(?,'recover',?,?,?,?,?)",
                    (str(owner_id or ""), request_id, str(request_hash or ""), value,
                     _dump({"status": "queued", "expected_version": int(expected_version)}), now),
                )
            return self._get_run(connection, value)

    def _accept_run_message_in_transaction(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        *,
        owner_id: str,
        client_message_id: str,
        content: str,
        expected_revision: int,
        allow_queued_resume: bool = False,
        source_report_id: str | None = None,
    ) -> dict[str, Any] | None:
        value = _validate_run_id(run_id)
        message_id = str(client_message_id or "").strip()
        body = str(content or "").strip()
        source_id = str(source_report_id or "").strip() or None
        if not message_id or len(message_id) > 128 or not body or len(body) > 20_000:
            raise ValueError("client_message_id/content 格式无效")
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        now = _now()
        row = connection.execute("SELECT * FROM runs WHERE run_id=?", (value,)).fetchone()
        if row is None or str(row["owner_id"] or "") != str(owner_id or ""):
            return None
        existing = connection.execute(
            "SELECT content_hash,revision,status,source_report_id FROM run_inputs WHERE run_id=? AND client_message_id=?",
            (value, message_id),
        ).fetchone()
        if existing is not None:
            if (not hmac.compare_digest(str(existing["content_hash"]), digest)
                    or str(existing["source_report_id"] or "") != str(source_id or "")):
                return None
            return {"status": str(existing["status"]), "client_message_id": message_id,
                    "revision": int(existing["revision"]), "idempotent": True}
        request = _load_json(row["request_json"], {}) or {}
        queued_resume = allow_queued_resume and row["status"] == "queued" and bool(request.get("resume_requested"))
        if row["status"] not in {"running", "waiting_user"} and not queued_resume:
            return None
        was_waiting = row["status"] == "waiting_user"
        if was_waiting and not _load_json(row["pending_input_json"]):
            return None
        runtime = _load_json(row["runtime_json"], {}) or {}
        current_revision = int(runtime.get("input_revision") or 0)
        if current_revision != int(expected_revision):
            return None
        revision = current_revision + 1
        runtime["input_revision"] = revision
        if was_waiting:
            request = {**request, "resume_requested": True, "input_answers": [],
                       "steering_client_message_id": message_id, "resume_ready": False}
        next_status = "queued" if was_waiting or queued_resume else "running"
        cursor = connection.execute(
            """UPDATE runs SET runtime_json=?,request_json=?,status=?,pending_input_json=CASE WHEN ? THEN NULL ELSE pending_input_json END,state_revision=state_revision+1,
               finished_at=CASE WHEN ? THEN NULL ELSE finished_at END,updated_at=?,version=version+1
               WHERE run_id=? AND status=? AND version=?""",
            (_dump(runtime), _dump(request), next_status, int(was_waiting), int(was_waiting), now,
             value, str(row["status"]), int(row["version"])),
        )
        if cursor.rowcount != 1:
            return None
        connection.execute(
            "INSERT INTO run_inputs(run_id,client_message_id,content,content_hash,revision,status,source_report_id) VALUES(?,?,?,?,?,'received',?)",
            (value, message_id, body, digest, revision, source_id),
        )
        self._append_event_in_transaction(connection, value,
            {"event_type": "message_received", "client_message_id": message_id, "revision": revision}, touch_run=False)
        connection.execute("UPDATE runs SET next_seq=next_seq+1 WHERE run_id=?", (value,))
        return {"status": "queued" if next_status == "queued" else "accepted", "client_message_id": message_id,
                "revision": revision, "idempotent": False}

    def accept_run_message(self, run_id: str, *, owner_id: str, client_message_id: str,
                           content: str, expected_revision: int) -> dict[str, Any] | None:
        value = _validate_run_id(run_id)
        with self._transaction() as connection:
            return self._accept_run_message_in_transaction(
                connection, value, owner_id=owner_id, client_message_id=client_message_id,
                content=content, expected_revision=expected_revision,
            )

    def pending_run_messages(self, run_id: str, *, lease_token: str | None = None) -> list[dict[str, Any]]:
        value = _validate_run_id(run_id)
        with self._transaction() as connection:
            row = connection.execute("SELECT * FROM runs WHERE run_id=?", (value,)).fetchone()
            if row is None or not self._lease_is_valid(row, lease_token):
                return []
            return [dict(item) for item in connection.execute(
                "SELECT client_message_id,content,revision,source_report_id FROM run_inputs WHERE run_id=? AND status='received' ORDER BY revision",
                (value,),
            ).fetchall()]

    def apply_run_messages(self, run_id: str, messages: list[dict[str, Any]], checkpoint: dict[str, Any],
                           *, lease_token: str | None = None) -> bool:
        value = _validate_run_id(run_id)
        now = _now()
        with self._transaction() as connection:
            row = connection.execute("SELECT * FROM runs WHERE run_id=?", (value,)).fetchone()
            if row is None or not self._lease_is_valid(row, lease_token, now):
                return False
            for message in messages:
                exists = connection.execute(
                    "SELECT 1 FROM run_inputs WHERE run_id=? AND client_message_id=? AND status='received'",
                    (value, str(message.get("client_message_id") or "")),
                ).fetchone()
                if exists is None:
                    return False
            for message in messages:
                message_id = str(message.get("client_message_id") or "")
                cursor = connection.execute(
                    "UPDATE run_inputs SET status='applied' WHERE run_id=? AND client_message_id=? AND status='received'",
                    (value, message_id),
                )
                if cursor.rowcount != 1:
                    return False
            connection.execute(
                "UPDATE runs SET checkpoint_json=?,updated_at=?,state_revision=state_revision+1,version=version+1 WHERE run_id=? AND status='running'",
                (_dump(_redact(checkpoint)), now, value),
            )
            for message in messages:
                self._append_event_in_transaction(connection, value, {
                    "event_type": "message_applied",
                    "client_message_id": str(message.get("client_message_id") or ""),
                    "revision": int(message.get("revision") or 0),
                }, touch_run=False)
                connection.execute("UPDATE runs SET next_seq=next_seq+1 WHERE run_id=?", (value,))
            return True

    def _after_event_insert(self) -> None:
        """Transaction seam for fault-injection tests and repository diagnostics."""

    def _append_event_in_transaction(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        event: dict[str, Any],
        *,
        touch_run: bool = True,
    ) -> dict[str, Any]:
        row = connection.execute("SELECT next_seq,attempt,state_revision FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"运行不存在：{run_id}")
        seq = int(row["next_seq"] or 1)
        record = {
            **_redact(event),
            "run_id": run_id,
            "attempt": max(0, int(row["attempt"] or 0)),
            "state_revision": max(0, int(row["state_revision"] or 0)),
            "event_seq": seq,
            "seq": seq,
        }
        connection.execute(
            "INSERT INTO run_events(run_id,seq,event_type,payload_json,created_at) VALUES(?,?,?,?,?)",
            (run_id, seq, str(record.get("event_type") or "message"), _dump(record), _now()),
        )
        self._after_event_insert()
        if touch_run:
            connection.execute(
                "UPDATE runs SET next_seq=?,updated_at=?,version=version+1 WHERE run_id=?",
                (seq + 1, _now(), run_id),
            )
        return record

    def append_run_event(
        self, run_id: str, event: dict[str, Any], *, lease_token: str | None = None
    ) -> dict[str, Any] | None:
        return self.append_leased_run_event(run_id, event, lease_token=lease_token)

    def append_leased_run_event(
        self, run_id: str, event: dict[str, Any], *, lease_token: str | None = None
    ) -> dict[str, Any] | None:
        value = _validate_run_id(run_id)
        if not isinstance(event, dict):
            raise ValueError("event 必须是对象")
        with self._transaction() as connection:
            row = connection.execute("SELECT * FROM runs WHERE run_id=?", (value,)).fetchone()
            if row is None:
                raise KeyError(f"运行不存在：{value}")
            if row["status"] in _TERMINAL:
                return None
            if not self._lease_is_valid(row, lease_token):
                return None
            return self._append_event_in_transaction(connection, value, event)

    def read_run_events(self, run_id: str, after_seq: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        value = _validate_run_id(run_id)
        page_size = max(1, min(1000, int(limit)))
        cursor = self._connect()
        try:
            rows = cursor.execute(
                "SELECT payload_json FROM run_events WHERE run_id=? AND seq>? ORDER BY seq LIMIT ?",
                (value, max(0, int(after_seq)), page_size),
            ).fetchall()
            return [_load_json(row["payload_json"], {}) for row in rows]
        finally:
            cursor.close()

    def run_status_cursor(self, run_id: str) -> tuple[str, int]:
        value = _validate_run_id(run_id)
        connection = self._connect()
        try:
            row = connection.execute("SELECT status,next_seq FROM runs WHERE run_id=?", (value,)).fetchone()
            if row is None:
                raise KeyError(f"运行不存在：{value}")
            return str(row["status"]), int(row["next_seq"] or 1) - 1
        finally:
            connection.close()

    def list_runs(self, session_id: str | None = None) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            if session_id is None:
                rows = connection.execute("SELECT run_id FROM runs ORDER BY updated_at DESC,run_id").fetchall()
            else:
                session = _validate_session_id(session_id)
                rows = connection.execute(
                    "SELECT run_id FROM runs WHERE session_id=? ORDER BY updated_at DESC,run_id",
                    (session,),
                ).fetchall()
            return [self._get_run(connection, row["run_id"]) for row in rows]
        finally:
            connection.close()

    def list_runs_owned(self, owner_id: str, session_id: str | None = None) -> list[dict[str, Any]]:
        if not owner_id:
            return []
        connection = self._connect()
        try:
            if session_id is None:
                rows = connection.execute(
                    "SELECT run_id FROM runs WHERE owner_id=? ORDER BY updated_at DESC,run_id",
                    (str(owner_id),),
                ).fetchall()
            else:
                session = _validate_session_id(session_id)
                rows = connection.execute(
                    "SELECT run_id FROM runs WHERE owner_id=? AND session_id=? ORDER BY updated_at DESC,run_id",
                    (str(owner_id), session),
                ).fetchall()
            return [self._get_run(connection, row["run_id"]) for row in rows]
        finally:
            connection.close()

    def finish_run(
        self,
        run_id: str,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        *,
        lease_token: str | None = None,
        failure_code: str | None = None,
        retryable: bool = False,
    ) -> dict[str, Any] | None:
        value = _validate_run_id(run_id)
        if error is None:
            validate_display_result(result)
        now = _now()
        with self._transaction() as connection:
            snapshot = self._get_run(connection, value)
            row = connection.execute("SELECT * FROM runs WHERE run_id=?", (value,)).fetchone()
            if snapshot["status"] in _TERMINAL:
                return snapshot
            if not self._lease_is_valid(row, lease_token, now):
                return None
            status = "error" if error else "done"
            safe_result = _redact(result) if result is not None else None
            next_state_revision = int(snapshot.get("state_revision") or 0) + 1
            runtime = _runtime_for_result(
                safe_result,
                snapshot.get("runtime"),
                status=status,
                state_revision=next_state_revision,
            )
            if failure_code == "worker_lost" and safe_result is None:
                connection.execute(
                    "UPDATE admission_usage SET actual_tokens=actual_tokens+reserved_tokens,reserved_tokens=0 WHERE run_id=?",
                    (value,),
                )
            else:
                self._settle_admission_usage(connection, value, safe_result)
            connection.execute(
                """UPDATE runs SET status=?,result_json=?,error=?,checkpoint_json=NULL,pending_input_json=NULL,state_revision=state_revision+1,
                   runtime_json=?,covered_event_seq=next_seq-1,finished_at=?,updated_at=?,failure_code=?,retryable=?,lease_token_hash=NULL,
                   lease_expires_at=NULL,version=version+1 WHERE run_id=?""",
                (status, _dump(safe_result) if safe_result is not None else None, str(error) if error else None,
                 _dump(runtime) if runtime is not None else None, now, now, failure_code,
                 1 if retryable else 0, value),
            )
            connection.execute(
                "UPDATE sessions SET active_run_id=NULL WHERE session_id=? AND active_run_id=?",
                (str(row["context_id"] or ""), value),
            )
            event: dict[str, Any] = {
                "event_type": "error" if error else "done",
                "error": str(error) if error else None,
                "failure_code": failure_code,
                "retryable": bool(retryable),
            }
            if not error:
                event["result"] = public_terminal_result(safe_result)
            terminal_event = self._append_event_in_transaction(connection, value, event, touch_run=False)
            connection.execute(
                "UPDATE runs SET next_seq=?,covered_event_seq=? WHERE run_id=?",
                (int(terminal_event["seq"]) + 1, int(terminal_event["seq"]), value),
            )
            return self._get_run(connection, value)

    def complete_run(
        self,
        run_id: str,
        result: dict[str, Any] | None = None,
        *,
        error: str | None = None,
        failure_code: str | None = None,
        retryable: bool = False,
        session_messages: list[dict[str, Any]] | None = None,
        session_run_id: str | None = None,
        lease_token: str | None = None,
    ) -> dict[str, Any] | None:
        """Commit the session delta, result, and terminal event under one lease."""
        value = _validate_run_id(run_id)
        if error is None:
            validate_display_result(result)
        if session_messages is not None and not isinstance(session_messages, list):
            raise ValueError("session_messages 必须是列表")
        messages = list(session_messages or [])
        if messages and not str(session_run_id or "").strip():
            raise ValueError("session_run_id is required when committing session messages")
        now = _now()
        with self._transaction() as connection:
            snapshot = self._get_run(connection, value)
            row = connection.execute("SELECT * FROM runs WHERE run_id=?", (value,)).fetchone()
            if snapshot["status"] in _TERMINAL:
                return snapshot
            if not self._lease_is_valid(row, lease_token, now):
                return None

            # A steering command may win the SQLite write lock just before
            # this completion transaction. In that case, do not publish the
            # now-stale answer: return the run to the durable queue with its
            # last safe checkpoint so the worker can apply the accepted input.
            pending_message = connection.execute(
                "SELECT 1 FROM run_inputs WHERE run_id=? AND status='received' LIMIT 1", (value,),
            ).fetchone()
            if pending_message is not None:
                next_state_revision = int(snapshot.get("state_revision") or 0) + 1
                runtime = _runtime_for_result(
                    None, snapshot.get("runtime"), status="queued", state_revision=next_state_revision,
                )
                connection.execute(
                    """UPDATE runs SET status='queued',result_json=NULL,error=NULL,failure_code=NULL,retryable=0,
                       finished_at=NULL,lease_token_hash=NULL,lease_expires_at=NULL,worker_id=NULL,
                       runtime_json=?,state_revision=state_revision+1,updated_at=?,version=version+1 WHERE run_id=?""",
                    (_dump(runtime) if runtime is not None else None, now, value),
                )
                return self._get_run(connection, value)

            context_id = str(row["context_id"] or "")
            committed_result = deepcopy(result) if isinstance(result, dict) else result
            report = committed_result.get("report") if isinstance(committed_result, dict) else None
            succeeded = error is None and isinstance(committed_result, dict) and committed_result.get("status") == "done"
            final_message = committed_result.get("final_message") if isinstance(committed_result, dict) else None
            final_text = str((final_message or {}).get("content") or "").strip() if isinstance(final_message, dict) else ""
            if succeeded and not final_text:
                final_text = str((report or {}).get("answer") or committed_result.get("summary") or "").strip() if isinstance(report, dict) else str(committed_result.get("summary") or "").strip()
            if succeeded and final_text and not any(
                item.get("role") == "assistant" and str(item.get("content") or "").strip() == final_text
                for item in messages if isinstance(item, dict)
            ):
                messages.append({"role": "assistant", "content": final_text})
            if messages and not str(session_run_id or "").strip():
                session_run_id = value
            next_context_revision = self._commit_session_delta(
                connection, row, messages, str(session_run_id or ""), now,
            )

            entry_ids: dict[str, str] = {}
            report_ref: dict[str, Any] | None = None
            execution_group = self._execution_group_payload(
                connection, value, snapshot,
                status="done" if succeeded else "error",
                finished_at=now,
            )
            if execution_group is not None:
                entry_ids["execution_group_entry_id"] = self._append_conversation_entry_in_transaction(
                    connection, context_id, "execution_group", f"run:{value}:execution",
                    execution_group, run_id=value, now=now,
                )
            if succeeded:
                # Entries use source keys stable across retries/replays. All
                # inserts happen under the same lease/context revision tx.
                if final_text:
                    entry_ids["assistant_entry_id"] = self._append_conversation_entry_in_transaction(
                        connection, context_id, "assistant_message", f"run:{value}:assistant",
                        {"text": final_text}, run_id=value, now=now,
                    )
                if isinstance(report, dict):
                    existing = connection.execute(
                        "SELECT report_id,version,parent_report_id,created_at FROM report_artifacts WHERE origin_run_id=?",
                        (value,),
                    ).fetchone()
                    if existing:
                        report_id, report_version = str(existing["report_id"]), int(existing["version"])
                        parent_report_id, report_created_at = existing["parent_report_id"], str(existing["created_at"])
                    else:
                        report_id = value
                        report_version = int(connection.execute(
                            "SELECT COALESCE(MAX(version),0)+1 FROM report_artifacts WHERE conversation_id=?",
                            (context_id,),
                        ).fetchone()[0])
                        parent_report_id = None
                        source_report_id = str(row["source_report_id"] or "") if "source_report_id" in row.keys() else ""
                        if source_report_id:
                            parent = connection.execute(
                                "SELECT report_id FROM report_artifacts WHERE report_id=? AND conversation_id=?",
                                (source_report_id, context_id),
                            ).fetchone()
                            if parent:
                                parent_report_id = source_report_id
                        report_created_at = now
                        evidence = report.get("answerEvidence") if isinstance(report.get("answerEvidence"), dict) else {}
                        from backend.agent_runtime.report_projection import report_context_text
                        context_text = report_context_text(
                            report, answer=final_text, report_id=report_id,
                            version=report_version, origin_run_id=value,
                        )
                        connection.execute(
                            """INSERT INTO report_artifacts(report_id,conversation_id,origin_run_id,version,parent_report_id,
                               report_schema_version,content_json,context_text,evidence_manifest_json,dataset_version,created_at)
                               VALUES(?,?,?,?,?,1,?,?,?,?,?)""",
                            (report_id, context_id, value, report_version, parent_report_id, _dump(report), context_text,
                             _dump(evidence), evidence.get("datasetVersion") if isinstance(evidence, dict) else None, now),
                        )
                    report_ref = {"report_id": report_id, "version": report_version,
                                  "origin_run_id": value, "parent_report_id": parent_report_id,
                                  "conversation_id": context_id, "created_at": report_created_at}
                    entry_ids["report_entry_id"] = self._append_conversation_entry_in_transaction(
                        connection, context_id, "report_card", f"run:{value}:report",
                        {"report_id": report_id, "version": report_version, "origin_run_id": value,
                         "title": str(report.get("summaryTitle") or report.get("scenarioLabel") or "分析报告"),
                         "summary": str(report.get("summaryText") or ""), "created_at": report_created_at},
                        run_id=value, now=now,
                    )
                    committed_result["report_ref"] = report_ref
                    connection.execute(
                        """UPDATE sessions SET selected_report_id=?,title=CASE
                           WHEN title='' OR title='新会话' THEN substr(?,1,80) ELSE title END,
                           title_source=CASE WHEN title='' OR title='新会话' THEN 'generated' ELSE title_source END
                           WHERE session_id=?""",
                        (report_id, str(row["input_text"] or "新会话"), context_id),
                    )
                if final_text and "assistant_entry_id" not in entry_ids:
                    entry_ids["assistant_entry_id"] = self._append_conversation_entry_in_transaction(
                        connection, context_id, "assistant_message", f"run:{value}:assistant",
                        {"text": final_text}, run_id=value, now=now,
                    )
                committed_result["entry_ids"] = entry_ids
                committed_result["conversation_id"] = context_id

            status = "error" if error else "done"
            safe_result = _redact(committed_result) if committed_result is not None else None
            next_state_revision = int(snapshot.get("state_revision") or 0) + 1
            runtime = _runtime_for_result(
                safe_result,
                snapshot.get("runtime"),
                status=status,
                state_revision=next_state_revision,
            )
            self._settle_admission_usage(connection, value, safe_result)
            connection.execute(
                """UPDATE runs SET status=?,result_json=?,error=?,checkpoint_json=NULL,pending_input_json=NULL,state_revision=state_revision+1,
                   runtime_json=?,context_revision=?,covered_event_seq=next_seq-1,finished_at=?,updated_at=?,failure_code=?,retryable=?,
                   lease_token_hash=NULL,lease_expires_at=NULL,worker_id=NULL,version=version+1 WHERE run_id=?""",
                (status, _dump(safe_result) if safe_result is not None else None,
                 str(error) if error else None, _dump(runtime) if runtime is not None else None,
                 next_context_revision, now, now, failure_code, 1 if retryable else 0, value),
            )
            connection.execute(
                "UPDATE sessions SET active_run_id=NULL WHERE session_id=? AND active_run_id=?",
                (context_id, value),
            )
            event: dict[str, Any] = {
                "event_type": "error" if error else "done",
                "error": str(error) if error else None,
                "failure_code": failure_code,
                "retryable": bool(retryable),
            }
            if not error:
                if report_ref:
                    event["report_ref"] = report_ref
                if entry_ids:
                    event["entry_ids"] = entry_ids
                event["result"] = public_terminal_result(safe_result)
            terminal_event = self._append_event_in_transaction(connection, value, event, touch_run=False)
            connection.execute(
                "UPDATE runs SET next_seq=?,covered_event_seq=? WHERE run_id=?",
                (int(terminal_event["seq"]) + 1, int(terminal_event["seq"]), value),
            )
            return self._get_run(connection, value)

    def cancel_run(self, run_id: str, *, lease_token: str | None = None) -> dict[str, Any] | None:
        value = _validate_run_id(run_id)
        now = _now()
        with self._transaction() as connection:
            snapshot = self._get_run(connection, value)
            if snapshot["status"] in _TERMINAL:
                return snapshot
            row = connection.execute("SELECT * FROM runs WHERE run_id=?", (value,)).fetchone()
            if snapshot["status"] == "running" and not self._lease_is_valid(row, lease_token, now):
                return None
            if snapshot["status"] == "running":
                connection.execute(
                    "UPDATE admission_usage SET actual_tokens=actual_tokens+reserved_tokens,reserved_tokens=0 WHERE run_id=?",
                    (value,),
                )
            else:
                self._settle_admission_usage(connection, value, None)
            connection.execute(
                """UPDATE runs SET status='cancelled',error=?,checkpoint_json=NULL,pending_input_json=NULL,finished_at=?,updated_at=?,cancel_requested_at=?,state_revision=state_revision+1,
                   lease_token_hash=NULL,lease_expires_at=NULL,version=version+1 WHERE run_id=?""",
                ("运行已取消。", now, now, now, value),
            )
            connection.execute(
                "UPDATE sessions SET active_run_id=NULL WHERE session_id=? AND active_run_id=?",
                (str(row["context_id"] or ""), value),
            )
            terminal_event = self._append_event_in_transaction(
                connection, value, {"event_type": "cancelled", "error": "运行已取消。"}, touch_run=False
            )
            connection.execute(
                "UPDATE runs SET next_seq=?,covered_event_seq=? WHERE run_id=?",
                (int(terminal_event["seq"]) + 1, int(terminal_event["seq"]), value),
            )
            return self._get_run(connection, value)

    def reconcile_runs(self, *, now_iso: str | None = None) -> list[dict[str, Any]]:
        now = now_iso or _now()
        changed: list[dict[str, Any]] = []
        with self._transaction() as connection:
            rows = connection.execute("SELECT * FROM runs WHERE status='running'").fetchall()
            for row in rows:
                expiry = row["lease_expires_at"]
                if expiry and self._time_value(expiry) > self._time_value(now):
                    continue
                run_id = str(row["run_id"])
                message = "运行工作进程丢失，可显式重试。"
                connection.execute(
                    """UPDATE runs SET status='error',error=?,failure_code='worker_lost',retryable=1,state_revision=state_revision+1,
                       finished_at=?,updated_at=?,lease_token_hash=NULL,lease_expires_at=NULL,version=version+1
                       WHERE run_id=? AND status='running'""",
                    (message, now, now, run_id),
                )
                terminal_event = self._append_event_in_transaction(
                    connection, run_id,
                    {"event_type": "error", "error": message, "failure_code": "worker_lost"},
                    touch_run=False,
                )
                connection.execute(
                    "UPDATE runs SET next_seq=?,covered_event_seq=? WHERE run_id=?",
                    (int(terminal_event["seq"]) + 1, int(terminal_event["seq"]), run_id),
                )
                connection.execute(
                    "UPDATE sessions SET active_run_id=NULL WHERE session_id=? AND active_run_id=?",
                    (str(row["context_id"] or ""), run_id),
                )
                changed.append(self._get_run(connection, run_id))
        return changed

    def _session_row(self, connection: sqlite3.Connection, session_id: str) -> sqlite3.Row | None:
        return connection.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()

    def _commit_session_delta(
        self,
        connection: sqlite3.Connection,
        run_row: sqlite3.Row,
        messages: list[dict[str, Any]],
        session_run_id: str,
        now: str,
    ) -> int:
        """Append one run's session delta and advance both context revisions."""
        context_id = str(run_row["context_id"] or "")
        run_id = str(run_row["run_id"])
        context_row = self._session_row(connection, context_id)
        if (
            context_row is None
            or str(context_row["owner_id"] or "") != str(run_row["owner_id"] or "")
            or str(context_row["active_run_id"] or "") != run_id
            or int(context_row["revision"] or 0) != int(run_row["context_revision"] or 0)
        ):
            raise RuntimeError("context_revision_conflict")

        next_context_revision = int(context_row["revision"] or 0)
        if not messages:
            return next_context_revision
        if connection.execute(
            "SELECT 1 FROM session_messages WHERE session_id=? AND run_id=? LIMIT 1",
            (context_id, session_run_id),
        ).fetchone():
            raise RuntimeError("session_delta_already_committed")
        parent_id = context_row["active_head_id"]
        ordinal_row = connection.execute(
            "SELECT COALESCE(MAX(ordinal),-1) AS ordinal FROM session_messages WHERE session_id=?",
            (context_id,),
        ).fetchone()
        ordinal = int(ordinal_row["ordinal"]) + 1
        for message in messages:
            record = SessionStore._record_message(message, parent_id, session_run_id)
            connection.execute(
                """INSERT INTO session_messages(message_id,session_id,parent_id,ordinal,run_id,created_at,
                   role,content,record_json) VALUES(?,?,?,?,?,?,?,?,?)""",
                (record["message_id"], context_id, record.get("parent_id"), ordinal,
                 record.get("run_id"), record["created_at"], record["role"],
                 _dump(record.get("content", "")), _dump(record)),
            )
            parent_id = record["message_id"]
            ordinal += 1
        next_context_revision += 1
        changed = connection.execute(
            """UPDATE sessions SET active_head_id=?,revision=?,updated_at=?,last_run_id=?
               WHERE session_id=? AND revision=?""",
            (parent_id, next_context_revision, now, session_run_id, context_id,
             int(context_row["revision"] or 0)),
        )
        if changed.rowcount != 1:
            raise RuntimeError("context_revision_conflict")
        return next_context_revision

    def _session_messages(self, connection: sqlite3.Connection, session_id: str) -> list[dict[str, Any]]:
        rows = connection.execute(
            "SELECT record_json FROM session_messages WHERE session_id=? ORDER BY ordinal",
            (session_id,),
        ).fetchall()
        messages = [_load_json(row["record_json"], {}) for row in rows]
        SessionStore._validate_records(messages)
        return messages

    def _session_snapshot(self, connection: sqlite3.Connection, session_id: str) -> SessionSnapshot:
        row = self._session_row(connection, session_id)
        if row is None:
            now = _now()
            return SessionSnapshot(session_id, created_at=now, updated_at=now)
        return SessionSnapshot(
            session_id=row["session_id"],
            owner_id=row["owner_id"],
            messages=self._session_messages(connection, session_id),
            active_head_id=row["active_head_id"],
            revision=int(row["revision"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_run_id=row["last_run_id"],
        )

    def load_session(self, session_id: str) -> SessionSnapshot:
        value = _validate_session_id(session_id)
        connection = self._connect()
        try:
            return self._session_snapshot(connection, value)
        finally:
            connection.close()

    def load_session_owned(self, session_id: str, owner_id: str) -> SessionSnapshot:
        value = _validate_session_id(session_id)
        connection = self._connect()
        try:
            row = self._session_row(connection, value)
            if row is None or not owner_id or str(row["owner_id"] or "") != str(owner_id):
                raise KeyError("会话不存在")
            return self._session_snapshot(connection, value)
        finally:
            connection.close()

    def active_session_records(self, session_id: str) -> list[dict[str, Any]]:
        snapshot = self.load_session(session_id)
        by_id = {str(item.get("message_id")): item for item in snapshot.messages}
        current = snapshot.active_head_id
        chain: list[dict[str, Any]] = []
        seen: set[str] = set()
        while current:
            if current in seen:
                raise RuntimeError("会话消息链存在循环")
            record = by_id.get(current)
            if record is None:
                raise RuntimeError("会话活动头指向不存在的消息")
            chain.append(deepcopy(record))
            seen.add(current)
            current = record.get("parent_id")
        chain.reverse()
        return chain

    def append_session_messages(
        self,
        session_id: str,
        messages: list[dict[str, Any]],
        *,
        run_id: str | None = None,
        owner_id: str | None = None,
        lease_token: str | None = None,
    ) -> SessionSnapshot:
        value = _validate_session_id(session_id)
        if not isinstance(messages, list):
            raise ValueError("messages 必须是列表")
        now = _now()
        with self._transaction() as connection:
            run_row = None
            if lease_token is not None:
                run_row = connection.execute("SELECT * FROM runs WHERE run_id=?", (str(run_id or ""),)).fetchone()
                if (
                    run_row is None
                    or str(run_row["context_id"] or "") != value
                    or str(run_row["owner_id"] or "") != str(owner_id or "")
                    or not self._lease_is_valid(run_row, lease_token, now)
                ):
                    raise RuntimeError("run_lease_conflict")
            row = self._session_row(connection, value)
            if row is None:
                connection.execute(
                    "INSERT INTO sessions(session_id,owner_id,revision,created_at,updated_at) VALUES(?,?,0,?,?)",
                    (value, str(owner_id) if owner_id is not None else None, now, now),
                )
                row = self._session_row(connection, value)
            elif owner_id is not None:
                if row["owner_id"] is not None and str(row["owner_id"]) != str(owner_id):
                    raise KeyError("会话不存在")
                if row["owner_id"] is None:
                    raise KeyError("会话不存在")
            if lease_token is not None and int(row["revision"] or 0) != int(run_row["context_revision"] or 0):
                raise RuntimeError("context_revision_conflict")
            if lease_token is not None and str(row["active_run_id"] or "") != str(run_id or ""):
                raise RuntimeError("run_lease_conflict")
            if run_id and connection.execute(
                "SELECT 1 FROM session_messages WHERE session_id=? AND run_id=? LIMIT 1", (value, str(run_id))
            ).fetchone():
                return self._session_snapshot(connection, value)
            if not messages:
                return self._session_snapshot(connection, value)
            parent_id = row["active_head_id"]
            ordinal_row = connection.execute(
                "SELECT COALESCE(MAX(ordinal),-1) AS ordinal FROM session_messages WHERE session_id=?", (value,)
            ).fetchone()
            ordinal = int(ordinal_row["ordinal"]) + 1
            for message in messages:
                record = SessionStore._record_message(message, parent_id, run_id)
                connection.execute(
                    """INSERT INTO session_messages(message_id,session_id,parent_id,ordinal,run_id,created_at,
                       role,content,record_json) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (record["message_id"], value, record.get("parent_id"), ordinal, record.get("run_id"),
                     record["created_at"], record["role"], _dump(record.get("content", "")), _dump(record)),
                )
                parent_id = record["message_id"]
                ordinal += 1
            connection.execute(
                "UPDATE sessions SET active_head_id=?,revision=revision+1,updated_at=?,last_run_id=COALESCE(?,last_run_id) WHERE session_id=?",
                (parent_id, now, str(run_id) if run_id else None, value),
            )
            if lease_token is not None:
                connection.execute(
                    "UPDATE runs SET context_revision=context_revision+1,updated_at=?,state_revision=state_revision+1,version=version+1 WHERE run_id=?",
                    (now, str(run_id)),
                )
            return self._session_snapshot(connection, value)

    def rollback_session(
        self,
        session_id: str,
        message_id: str,
        *,
        expected_revision: int | None = None,
        owner_id: str | None = None,
    ) -> SessionSnapshot:
        value = _validate_session_id(session_id)
        with self._transaction() as connection:
            row = self._session_row(connection, value)
            if row is None or (owner_id is not None and str(row["owner_id"] or "") != str(owner_id)):
                raise KeyError("会话不存在")
            revision = int(row["revision"])
            if expected_revision is not None and revision != int(expected_revision):
                raise ValueError("会话 revision 已变化，请重新读取后再回滚")
            snapshot = self._session_snapshot(connection, value)
            by_id = {str(item.get("message_id")): item for item in snapshot.messages}
            active_ids: set[str] = set()
            current = snapshot.active_head_id
            seen: set[str] = set()
            while current:
                if current in seen:
                    raise RuntimeError("会话消息链存在循环")
                record = by_id.get(current)
                if record is None:
                    raise RuntimeError("会话活动头指向不存在的消息")
                active_ids.add(current)
                seen.add(current)
                current = record.get("parent_id")
            target = by_id.get(str(message_id or ""))
            if target is None or str(message_id) not in active_ids:
                raise ValueError("只能回滚到当前活动分支中的消息")
            connection.execute(
                "UPDATE sessions SET active_head_id=?,revision=?,updated_at=? WHERE session_id=? AND revision=?",
                (target.get("parent_id"), revision + 1, _now(), value, revision),
            )
            return self._session_snapshot(connection, value)

    def import_legacy_batch(
        self,
        runs: list[tuple[str, str, str, dict[str, Any]]],
        sessions: list[tuple[str, str, str, dict[str, Any]]],
        *,
        legacy_owner_id: str,
    ) -> tuple[int, int]:
        imported = skipped = 0
        now = _now()
        with self._transaction() as connection:
            for kind, records in (("run", runs), ("session", sessions)):
                for source_key, content_hash, source_path, payload in records:
                    known = connection.execute(
                        "SELECT content_hash FROM legacy_sources WHERE source_key=?", (source_key,)
                    ).fetchone()
                    if known is not None:
                        if known["content_hash"] != content_hash:
                            raise ValueError(f"conflicting legacy source: {source_path}")
                        skipped += 1
                        continue
                    if kind == "run":
                        self._import_run_in_transaction(connection, payload, legacy_owner_id)
                    else:
                        self._import_session_in_transaction(connection, payload, legacy_owner_id)
                    connection.execute(
                        "INSERT INTO legacy_sources(source_key,content_hash,source_path,imported_at) VALUES(?,?,?,?)",
                        (source_key, content_hash, source_path, now),
                    )
                    imported += 1
        return imported, skipped

    def _import_run_in_transaction(self, connection: sqlite3.Connection, payload: dict[str, Any], owner_id: str) -> None:
        run_id = _validate_run_id(str(payload.get("run_id") or ""))
        session_id = _validate_session_id(str(payload.get("session_id") or ""))
        context_id = _validate_session_id(str(payload.get("context_id") or session_id))
        if connection.execute("SELECT 1 FROM runs WHERE run_id=?", (run_id,)).fetchone():
            raise ValueError(f"legacy run conflicts with existing run: {run_id}")
        now = _now()
        created = str(payload.get("created_at") or now)
        updated = str(payload.get("updated_at") or created)
        connection.execute(
            """INSERT INTO runs(run_id,owner_id,session_id,context_id,input_text,status,next_seq,resume_revision,
               result_json,error,checkpoint_json,pending_input_json,runtime_json,created_at,updated_at,queued_at,
               started_at,heartbeat_at,finished_at,attempt,worker_id,lease_token_hash,lease_expires_at,failure_code,
               retryable,cancel_requested_at,version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (run_id, owner_id, session_id, context_id, str(payload.get("input") or ""),
             str(payload.get("status") or "queued"), int(payload.get("next_seq") or 1), int(payload.get("resume_revision") or 0),
             _dump(payload.get("result")) if payload.get("result") is not None else None, payload.get("error"),
             _dump(payload.get("checkpoint")) if payload.get("checkpoint") is not None else None,
             _dump(payload.get("pending_input")) if payload.get("pending_input") is not None else None,
             _dump(payload.get("runtime")) if payload.get("runtime") is not None else None,
             created, updated, payload.get("queued_at") or created, payload.get("started_at"), payload.get("heartbeat_at"),
             payload.get("finished_at"), int(payload.get("attempt") or 0), payload.get("worker_id"),
             payload.get("lease_token_hash"), payload.get("lease_expires_at"), payload.get("failure_code"),
             int(bool(payload.get("retryable"))), payload.get("cancel_requested_at"), int(payload.get("version") or 0)),
        )
        events = payload.get("events") or []
        if not isinstance(events, list):
            raise ValueError(f"legacy run events must be a list: {run_id}")
        seen_seq: set[int] = set()
        for event in events:
            if not isinstance(event, dict):
                raise ValueError(f"legacy run event must be an object: {run_id}")
            seq = int(event.get("seq") or 0)
            if seq <= 0 or seq in seen_seq:
                raise ValueError(f"legacy run event sequence conflict: {run_id}")
            seen_seq.add(seq)
            connection.execute(
                "INSERT INTO run_events(run_id,seq,event_type,payload_json,created_at) VALUES(?,?,?,?,?)",
                (run_id, seq, str(event.get("event_type") or "message"), _dump(event), str(event.get("created_at") or updated)),
            )

    def _import_session_in_transaction(self, connection: sqlite3.Connection, payload: dict[str, Any], owner_id: str) -> None:
        session_id = _validate_session_id(str(payload.get("session_id") or ""))
        if connection.execute("SELECT 1 FROM sessions WHERE session_id=?", (session_id,)).fetchone():
            raise ValueError(f"legacy session conflicts with existing session: {session_id}")
        now = _now()
        created = str(payload.get("created_at") or now)
        updated = str(payload.get("updated_at") or created)
        records = payload.get("messages") or []
        if not isinstance(records, list):
            raise ValueError(f"legacy session messages must be a list: {session_id}")
        connection.execute(
            "INSERT INTO sessions(session_id,owner_id,active_head_id,revision,created_at,updated_at,last_run_id) VALUES(?,?,?,?,?,?,?)",
            (session_id, owner_id, payload.get("active_head_id"), int(payload.get("revision") or 0), created, updated, payload.get("last_run_id")),
        )
        for ordinal, raw in enumerate(records):
            if not isinstance(raw, dict):
                raise ValueError(f"legacy session message must be an object: {session_id}")
            record = dict(raw)
            if not record.get("message_id"):
                record["message_id"] = f"legacy-{session_id}-{ordinal}"
            record.setdefault("parent_id", records[ordinal - 1].get("message_id") if ordinal and isinstance(records[ordinal - 1], dict) else None)
            record.setdefault("created_at", created)
            record.setdefault("run_id", payload.get("last_run_id"))
            role = str(record.get("role") or "")
            if role not in {"user", "assistant", "tool"}:
                raise ValueError(f"legacy session role invalid: {session_id}")
            connection.execute(
                """INSERT INTO session_messages(message_id,session_id,parent_id,ordinal,run_id,created_at,
                   role,content,record_json) VALUES(?,?,?,?,?,?,?,?,?)""",
                (str(record["message_id"]), session_id, record.get("parent_id"), ordinal, record.get("run_id"),
                 str(record["created_at"]), role, _dump(record.get("content", "")), _dump(record)),
            )


class SQLiteRunStore:
    """RunStore-compatible facade used by the HTTP service."""

    def __init__(self, repository: SQLiteRuntimeRepository) -> None:
        self.repository = repository

    def create(self, run_id: str, session_id: str, input_text: str, context_id: str | None = None, owner_id: str | None = None, request_data: dict[str, Any] | None = None, client_request_id: str | None = None, idempotency_operation: str = "submit", admission_limits: dict[str, int] | None = None, reserve_tokens: int = 0) -> dict[str, Any]:
        return self.repository.create_run(
            run_id, session_id, input_text, context_id=context_id, owner_id=owner_id,
            request_data=request_data, client_request_id=client_request_id,
            idempotency_operation=idempotency_operation,
            admission_limits=admission_limits, reserve_tokens=reserve_tokens,
        )

    def update_request(self, run_id: str, request_data: dict[str, Any]) -> dict[str, Any]:
        return self.repository.update_run_request(run_id, request_data)

    def prepare_resume(self, run_id: str, request_data: dict[str, Any], *, expected_tool_call_id: str | None = None, expected_revision: int | None = None, client_request_id: str | None = None, request_hash: str | None = None) -> bool:
        return self.repository.prepare_run_resume(
            run_id, request_data, expected_tool_call_id=expected_tool_call_id, expected_revision=expected_revision,
            client_request_id=client_request_id, request_hash=request_hash,
        )

    def load(self, run_id: str) -> dict[str, Any]:
        return self.repository.get_run(run_id)

    def load_owned(self, run_id: str, owner_id: str) -> dict[str, Any]:
        return self.repository.get_run_owned(run_id, owner_id)

    def start(self, run_id: str) -> dict[str, Any]:
        return self.repository.start_run(run_id)

    def claim_start(self, run_id: str) -> bool:
        return self.repository.claim_start(run_id)

    def claim_waiting(self, run_id: str, *, expected_tool_call_id: str | None = None, expected_revision: int | None = None) -> bool:
        return self.repository.claim_waiting(run_id, expected_tool_call_id=expected_tool_call_id, expected_revision=expected_revision)

    def pause(self, run_id: str, checkpoint: dict[str, Any], pending_input: dict[str, Any], runtime: dict[str, Any] | None = None, result: dict[str, Any] | None = None, *, waiting_event: dict[str, Any] | None = None, session_messages: list[dict[str, Any]] | None = None, session_run_id: str | None = None, lease_token: str | None = None) -> dict[str, Any] | None:
        return self.repository.pause_run(run_id, checkpoint, pending_input, runtime, result, waiting_event=waiting_event, session_messages=session_messages, session_run_id=session_run_id, lease_token=lease_token)

    def append(self, run_id: str, event: dict[str, Any], *, lease_token: str | None = None) -> dict[str, Any] | None:
        return self.repository.append_leased_run_event(run_id, event, lease_token=lease_token)

    def read_after(self, run_id: str, after_seq: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        return self.repository.read_run_events(run_id, after_seq, limit)

    def status_cursor(self, run_id: str) -> tuple[str, int]:
        return self.repository.run_status_cursor(run_id)

    def list(self, session_id: str | None = None) -> list[dict[str, Any]]:
        return self.repository.list_runs(session_id)

    def list_owned(self, owner_id: str, session_id: str | None = None) -> list[dict[str, Any]]:
        return self.repository.list_runs_owned(owner_id, session_id)

    def finish(self, run_id: str, result: dict[str, Any] | None = None, error: str | None = None, *, lease_token: str | None = None, failure_code: str | None = None, retryable: bool = False) -> dict[str, Any] | None:
        return self.repository.finish_run(run_id, result, error, lease_token=lease_token, failure_code=failure_code, retryable=retryable)

    def cancel(self, run_id: str, *, lease_token: str | None = None) -> dict[str, Any] | None:
        return self.repository.cancel_run(run_id, lease_token=lease_token)


class SQLiteSessionStore:
    """SessionStore-compatible facade backed by the shared SQLite repository."""

    def __init__(self, repository: SQLiteRuntimeRepository) -> None:
        self.repository = repository

    def load(self, session_id: str) -> SessionSnapshot:
        return self.repository.load_session(session_id)

    def load_owned(self, session_id: str, owner_id: str) -> SessionSnapshot:
        return self.repository.load_session_owned(session_id, owner_id)

    def active_records(self, session_id: str) -> list[dict[str, Any]]:
        return self.repository.active_session_records(session_id)

    def active_records_owned(self, session_id: str, owner_id: str) -> list[dict[str, Any]]:
        self.repository.load_session_owned(session_id, owner_id)
        return self.repository.active_session_records(session_id)

    def active_messages(self, session_id: str, owner_id: str | None = None) -> list[dict[str, Any]]:
        records = self.active_records_owned(session_id, owner_id) if owner_id is not None else self.active_records(session_id)
        return [SessionStore._public_message(record) for record in records]

    def append_messages(self, session_id: str, messages: list[dict[str, Any]], run_id: str | None = None, owner_id: str | None = None, lease_token: str | None = None) -> SessionSnapshot:
        return self.repository.append_session_messages(session_id, messages, run_id=run_id, owner_id=owner_id, lease_token=lease_token)

    def rollback(self, session_id: str, message_id: str, expected_revision: int | None = None, owner_id: str | None = None) -> SessionSnapshot:
        return self.repository.rollback_session(session_id, message_id, expected_revision=expected_revision, owner_id=owner_id)
