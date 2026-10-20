import json

import pytest

from backend.agent_runtime.sqlite_repository import SQLiteRuntimeRepository
from backend.migrations.import_json_runtime import MigrationConflict, import_json_runtime


def test_json_import_is_idempotent_and_preserves_run_events_and_session_branch(tmp_path):
    runs = tmp_path / "legacy-runs"
    sessions = tmp_path / "legacy-sessions"
    runs.mkdir()
    sessions.mkdir()
    (runs / "run-legacy.json").write_text(
        json.dumps(
            {
                "run_id": "run-legacy",
                "session_id": "session-legacy",
                "context_id": "context-legacy",
                "input": "查询宝马",
                "status": "done",
                "events": [{"seq": 1, "event_type": "started"}, {"seq": 2, "event_type": "done", "result": {"summary": "完成"}}],
                "next_seq": 3,
                "resume_revision": 0,
                "result": {"summary": "完成"},
                "error": None,
                "created_at": "2026-09-01T00:00:00+00:00",
                "updated_at": "2026-09-01T00:00:01+00:00",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (sessions / "session-legacy.json").write_text(
        json.dumps(
            {
                "session_id": "session-legacy",
                "messages": [
                    {"message_id": "message-1", "parent_id": None, "run_id": "run-legacy", "role": "user", "content": "查询宝马"},
                    {"message_id": "message-2", "parent_id": "message-1", "run_id": "run-legacy", "role": "assistant", "content": "完成"},
                ],
                "active_head_id": "message-1",
                "revision": 1,
                "created_at": "2026-09-01T00:00:00+00:00",
                "updated_at": "2026-09-01T00:00:01+00:00",
                "last_run_id": "run-legacy",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / "runtime.sqlite3"

    first = import_json_runtime(runs, sessions, db_path, legacy_owner_id="legacy-owner")
    second = import_json_runtime(runs, sessions, db_path, legacy_owner_id="legacy-owner")
    repository = SQLiteRuntimeRepository(db_path)
    run = repository.get_run_owned("run-legacy", "legacy-owner")
    session = repository.load_session_owned("session-legacy", "legacy-owner")

    assert first.imported == 2
    assert first.skipped == 0
    assert second.imported == 0
    assert second.skipped == 2
    assert run["context_id"] == "context-legacy"
    assert [item["seq"] for item in repository.read_run_events("run-legacy")] == [1, 2]
    assert session.active_head_id == "message-1"
    assert session.revision == 1
    assert len(session.messages) == 2
    assert run["owner_id"] == session.owner_id == "legacy-owner"


def test_json_import_conflicting_source_content_rolls_back_the_import(tmp_path):
    runs = tmp_path / "legacy-runs"
    sessions = tmp_path / "legacy-sessions"
    runs.mkdir()
    sessions.mkdir()
    run_path = runs / "run-conflict.json"
    run_path.write_text(json.dumps({"run_id": "run-conflict", "session_id": "session-conflict", "events": []}), encoding="utf-8")
    db_path = tmp_path / "runtime.sqlite3"
    import_json_runtime(runs, sessions, db_path, legacy_owner_id="legacy-owner")
    run_path.write_text(json.dumps({"run_id": "run-conflict", "session_id": "session-conflict", "input": "changed", "events": []}), encoding="utf-8")
    (runs / "run-new.json").write_text(json.dumps({"run_id": "run-new", "session_id": "session-new", "events": []}), encoding="utf-8")

    with pytest.raises(MigrationConflict):
        import_json_runtime(runs, sessions, db_path, legacy_owner_id="legacy-owner")

    repository = SQLiteRuntimeRepository(db_path)
    with pytest.raises(KeyError):
        repository.get_run("run-new")
