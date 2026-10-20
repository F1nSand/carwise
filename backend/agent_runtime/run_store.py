"""可回放的 Agent 运行事件存储。"""

from __future__ import annotations

import json
import hashlib
import os
import re
import threading
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from backend.agent_runtime.state import _safe_value
from backend.agent_runtime.run_contracts import validate_display_result, validate_pending_input
from backend.api_projection import public_terminal_result
from backend.config import ROOT

_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_TERMINAL = {"done", "error", "cancelled"}


def _validate_run_id(run_id: str) -> str:
    value = str(run_id or "").strip()
    if not _RUN_ID.fullmatch(value):
        raise ValueError("run_id 必须是 1-128 位字母、数字、下划线或短横线")
    return value


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key).lower().replace("-", "_")
            result[str(key)] = "[REDACTED]" if key_text in {"api_key", "authorization", "x_api_key"} else _redact(item)
        return result
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return _safe_value(value)


def canonical_request_hash(value: dict[str, Any]) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class RunStore:
    """每个 run 一个原子 JSON 文件，事件序号从 1 单调递增。"""

    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root) if root is not None else ROOT / ".cache" / "runs"
        self.root.mkdir(parents=True, exist_ok=True)
        self._locks: dict[str, threading.RLock] = {}
        self._guard = threading.Lock()
        self._request_lock = threading.RLock()

    def _lock(self, run_id: str) -> threading.RLock:
        with self._guard:
            return self._locks.setdefault(run_id, threading.RLock())

    def _path(self, run_id: str) -> Path:
        return self.root / f"{_validate_run_id(run_id)}.json"

    def create(
        self,
        run_id: str,
        session_id: str,
        input_text: str,
        context_id: str | None = None,
        owner_id: str | None = None,
        request_data: dict[str, Any] | None = None,
        client_request_id: str | None = None,
        idempotency_operation: str = "submit",
        admission_limits: dict[str, int] | None = None,
        reserve_tokens: int = 0,
    ) -> dict[str, Any]:
        value = _validate_run_id(run_id)
        path = self._path(value)
        request_id = str(client_request_id or "").strip()
        if len(request_id) > 128 or any(ord(char) < 32 for char in request_id):
            raise ValueError("client_request_id 格式无效")
        request_hash = canonical_request_hash({
            "operation": idempotency_operation,
            "session_id": str(session_id),
            "context_id": str(context_id or session_id),
            "input": str(input_text),
            "request": request_data or {},
        })
        with self._request_lock, self._lock(value):
            if request_id:
                for saved in self.list():
                    if (str(saved.get("owner_id") or "") == str(owner_id or "")
                            and saved.get("client_request_id") == request_id
                            and saved.get("idempotency_operation", "submit") == idempotency_operation):
                        if saved.get("client_request_hash") != request_hash:
                            raise ValueError("client_request_conflict")
                        return saved
            if path.exists():
                existing = self.load(value)
                if owner_id is not None and str(existing.get("owner_id") or "") != str(owner_id):
                    raise KeyError("运行不存在")
                if (
                    str(existing.get("session_id") or "") != str(session_id)
                    or str(existing.get("context_id") or existing.get("session_id") or "") != str(context_id or session_id)
                    or str(existing.get("input") or "") != str(input_text)
                    or (existing.get("request") or {}) != (request_data or {})
                ):
                    raise ValueError("run_id_conflict")
                if request_id:
                    existing["client_request_id"] = request_id
                    existing["client_request_hash"] = request_hash
                    existing["idempotency_operation"] = idempotency_operation
                    self._touch_write(value, existing)
                return existing
            now = datetime.now(UTC).isoformat()
            snapshot = {
                "run_id": value,
                "owner_id": str(owner_id) if owner_id is not None else None,
                "session_id": str(session_id),
                "context_id": str(context_id or session_id),
                "input": str(input_text),
                "request": deepcopy(request_data or {}),
                "client_request_id": request_id or None,
                "client_request_hash": request_hash if request_id else None,
                "idempotency_operation": idempotency_operation,
                "status": "queued",
                "state_revision": 0,
                "events": [],
                "next_seq": 1,
                "resume_revision": 0,
                "result": None,
                "error": None,
                "created_at": now,
                "updated_at": now,
            }
            self._write(value, snapshot)
            return deepcopy(snapshot)

    def update_request(self, run_id: str, request_data: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(request_data, dict):
            raise ValueError("request_data 必须是对象")
        value = _validate_run_id(run_id)
        with self._lock(value):
            snapshot = self.load(value)
            snapshot["request"] = deepcopy(request_data)
            snapshot["state_revision"] = int(snapshot.get("state_revision") or 0) + 1
            self._touch_write(value, snapshot)
            return deepcopy(snapshot)

    def load(self, run_id: str) -> dict[str, Any]:
        value = _validate_run_id(run_id)
        with self._lock(value):
            try:
                payload = json.loads(self._path(value).read_text(encoding="utf-8"))
            except FileNotFoundError as exc:
                raise KeyError(f"运行不存在：{value}") from exc
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RuntimeError("运行快照无法安全读取") from exc
        if not isinstance(payload, dict) or payload.get("run_id") != value:
            raise RuntimeError("运行快照格式非法")
        # Older local snapshots predate the explicit Agent context boundary.
        # Keep them readable and make their historical session the safe fallback.
        payload.setdefault("context_id", str(payload.get("session_id") or ""))
        payload.setdefault("request", {})
        payload.setdefault("state_revision", int(payload.get("version") or 0))
        return payload

    def load_owned(self, run_id: str, owner_id: str) -> dict[str, Any]:
        snapshot = self.load(run_id)
        if not owner_id or str(snapshot.get("owner_id") or "") != str(owner_id):
            raise KeyError("运行不存在")
        return snapshot

    def start(self, run_id: str) -> dict[str, Any]:
        value = _validate_run_id(run_id)
        with self._lock(value):
            snapshot = self.load(value)
            if snapshot["status"] not in _TERMINAL:
                snapshot["status"] = "running"
                snapshot["state_revision"] = int(snapshot.get("state_revision") or 0) + 1
                self._touch_write(value, snapshot)
            return deepcopy(snapshot)

    def claim_start(self, run_id: str) -> bool:
        """原子认领 queued run；已被其他请求/worker认领或已结束则返回 False。"""
        value = _validate_run_id(run_id)
        with self._lock(value):
            snapshot = self.load(value)
            if snapshot.get("status") != "queued":
                return False
            snapshot["status"] = "running"
            snapshot["state_revision"] = int(snapshot.get("state_revision") or 0) + 1
            self._touch_write(value, snapshot)
            return True

    def claim_waiting(
        self,
        run_id: str,
        *,
        expected_tool_call_id: str | None = None,
        expected_revision: int | None = None,
    ) -> bool:
        """原子认领 waiting_user run，避免同一问题被并发恢复两次。"""
        value = _validate_run_id(run_id)
        with self._lock(value):
            snapshot = self.load(value)
            if snapshot.get("status") != "waiting_user":
                return False
            pending = snapshot.get("pending_input") or {}
            if expected_tool_call_id is not None and str(pending.get("tool_call_id") or "") != str(expected_tool_call_id):
                return False
            current_revision = int(snapshot.get("resume_revision") or 0)
            if expected_revision is not None and current_revision != int(expected_revision):
                return False
            snapshot["status"] = "running"
            snapshot["state_revision"] = int(snapshot.get("state_revision") or 0) + 1
            snapshot["resume_revision"] = current_revision + 1
            runtime = snapshot.get("runtime")
            if isinstance(runtime, dict):
                runtime = dict(runtime)
                runtime["status"] = "running"
                snapshot["runtime"] = runtime
            self._touch_write(value, snapshot)
            return True

    def pause(
        self,
        run_id: str,
        checkpoint: dict[str, Any],
        pending_input: dict[str, Any],
        runtime: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
        *,
        waiting_event: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """原子保存等待状态与其可回放通知。"""
        value = _validate_run_id(run_id)
        validate_pending_input(pending_input)
        if not isinstance(checkpoint, dict):
            raise ValueError("waiting checkpoint 必须是对象")
        if waiting_event is not None and not isinstance(waiting_event, dict):
            raise ValueError("waiting_event 必须是对象")
        with self._lock(value):
            snapshot = self.load(value)
            if snapshot["status"] in _TERMINAL:
                return deepcopy(snapshot)
            snapshot["status"] = "waiting_user"
            snapshot["state_revision"] = int(snapshot.get("state_revision") or 0) + 1
            snapshot.setdefault("resume_revision", 0)
            snapshot["checkpoint"] = _redact(checkpoint)
            snapshot["pending_input"] = _redact(pending_input)
            snapshot["runtime"] = _redact(runtime or {"status": "waiting_user"})
            if result is not None:
                snapshot["result"] = _redact(result)
            if waiting_event is not None:
                record = {
                    **_redact({**waiting_event, "resume_revision": int(snapshot.get("resume_revision") or 0)}),
                    "seq": int(snapshot.get("next_seq") or 1),
                }
                snapshot.setdefault("events", []).append(record)
                snapshot["next_seq"] = record["seq"] + 1
            self._touch_write(value, snapshot)
            return deepcopy(snapshot)

    def append(self, run_id: str, event: dict[str, Any]) -> dict[str, Any]:
        value = _validate_run_id(run_id)
        if not isinstance(event, dict):
            raise ValueError("event 必须是对象")
        with self._lock(value):
            snapshot = self.load(value)
            record = {"seq": int(snapshot.get("next_seq") or 1), **_redact(event)}
            snapshot.setdefault("events", []).append(record)
            snapshot["next_seq"] = record["seq"] + 1
            self._touch_write(value, snapshot)
            return deepcopy(record)

    def read_after(self, run_id: str, after_seq: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        value = _validate_run_id(run_id)
        cursor = max(0, int(after_seq))
        page_size = max(1, min(1000, int(limit)))
        snapshot = self.load(value)
        return deepcopy([event for event in snapshot.get("events", []) if int(event.get("seq", 0)) > cursor][:page_size])

    def status_cursor(self, run_id: str) -> tuple[str, int]:
        snapshot = self.load(run_id)
        return str(snapshot.get("status") or "queued"), int(snapshot.get("next_seq") or 1) - 1

    def list(self, session_id: str | None = None) -> list[dict[str, Any]]:
        """列出运行快照；可按受控 session_id 过滤并按更新时间倒序。"""
        session_filter = _validate_run_id(session_id) if session_id is not None else None
        snapshots: list[dict[str, Any]] = []
        for path in self.root.glob("*.json"):
            try:
                run_id = _validate_run_id(path.stem)
                snapshot = self.load(run_id)
            except (KeyError, OSError, RuntimeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            if session_filter is not None and str(snapshot.get("session_id")) != session_filter:
                continue
            snapshots.append(snapshot)
        snapshots.sort(
            key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""),
            reverse=True,
        )
        return deepcopy(snapshots)

    def list_owned(self, owner_id: str, session_id: str | None = None) -> list[dict[str, Any]]:
        if not owner_id:
            return []
        return [
            snapshot
            for snapshot in self.list(session_id)
            if str(snapshot.get("owner_id") or "") == str(owner_id)
        ]

    def finish(
        self,
        run_id: str,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        *,
        failure_code: str | None = None,
        retryable: bool = False,
    ) -> dict[str, Any]:
        value = _validate_run_id(run_id)
        if error is None:
            validate_display_result(result)
        with self._lock(value):
            snapshot = self.load(value)
            if snapshot["status"] in _TERMINAL:
                return deepcopy(snapshot)
            snapshot["status"] = "error" if error else "done"
            snapshot["state_revision"] = int(snapshot.get("state_revision") or 0) + 1
            safe_result = _redact(result) if result is not None else None
            snapshot["result"] = safe_result
            snapshot["error"] = str(error) if error else None
            snapshot["failure_code"] = failure_code
            snapshot["retryable"] = bool(retryable)
            snapshot["checkpoint"] = None
            snapshot["pending_input"] = None
            if isinstance(safe_result, dict):
                runtime_state = safe_result.get("state") or safe_result.get("runtime")
                if isinstance(runtime_state, dict):
                    snapshot["runtime"] = runtime_state
            terminal_event: dict[str, Any] = {
                "event_type": "error" if error else "done",
                "error": str(error) if error else None,
                "failure_code": failure_code,
                "retryable": bool(retryable),
            }
            if not error:
                terminal_event["result"] = public_terminal_result(safe_result)
            record = {"seq": int(snapshot.get("next_seq") or 1), **_redact(terminal_event)}
            snapshot.setdefault("events", []).append(record)
            snapshot["next_seq"] = record["seq"] + 1
            self._touch_write(value, snapshot)
            return deepcopy(snapshot)

    def cancel(self, run_id: str) -> dict[str, Any]:
        value = _validate_run_id(run_id)
        with self._lock(value):
            snapshot = self.load(value)
            if snapshot.get("status") in _TERMINAL:
                return deepcopy(snapshot)
            snapshot["status"] = "cancelled"
            snapshot["state_revision"] = int(snapshot.get("state_revision") or 0) + 1
            snapshot["error"] = "运行已取消。"
            snapshot["checkpoint"] = None
            snapshot["pending_input"] = None
            snapshot["finished_at"] = datetime.now(UTC).isoformat()
            record = {"seq": int(snapshot.get("next_seq") or 1), "event_type": "cancelled", "error": "运行已取消。"}
            snapshot.setdefault("events", []).append(record)
            snapshot["next_seq"] = record["seq"] + 1
            self._touch_write(value, snapshot)
            return deepcopy(snapshot)

    def _touch_write(self, run_id: str, snapshot: dict[str, Any]) -> None:
        snapshot["updated_at"] = datetime.now(UTC).isoformat()
        self._write(run_id, snapshot)

    def _write(self, run_id: str, snapshot: dict[str, Any]) -> None:
        path = self._path(run_id)
        tmp = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            tmp.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise RuntimeError("运行事件写入失败") from exc
