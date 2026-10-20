from concurrent.futures import ThreadPoolExecutor

import pytest

from backend.agent_runtime.repositories import RunRepository, SessionRepository
from backend.agent_runtime.sqlite_repository import SQLiteRuntimeRepository
from backend.agent_runtime.run_store import canonical_request_hash
from backend.api_projection import public_run_snapshot


def test_conversation_schema_migration_and_create_command_replay_are_idempotent(tmp_path):
    path = tmp_path / "conversation-migration.sqlite3"
    repository = SQLiteRuntimeRepository(path)
    repository.create_run("legacy-run-a", "browser-group", "旧问题 A", context_id="context-a", owner_id="owner-a")
    repository.create_run("legacy-run-b", "browser-group", "旧问题 B", context_id="context-b", owner_id="owner-a")
    repository = SQLiteRuntimeRepository(path)
    repository = SQLiteRuntimeRepository(path)

    created = repository.create_conversation("owner-a", "create-once")
    replay = repository.create_conversation("owner-a", "create-once")
    assert replay["conversation_id"] == created["conversation_id"]
    assert repository.get_conversation_owned("context-a", "owner-a")["conversation_id"] == "context-a"
    listed = repository.list_conversations_owned("owner-a")
    ids = {item["conversation_id"] for item in listed["items"]}
    assert {"context-a", "context-b", created["conversation_id"]} <= ids
    assert repository._connect().execute("PRAGMA user_version").fetchone()[0] == 10


def test_v7_backfill_creates_stable_timeline_and_versioned_report_with_backup(tmp_path):
    import json

    path = tmp_path / "legacy-v7.sqlite3"
    repository = SQLiteRuntimeRepository(path)
    repository.create_run("legacy-complete", "browser-group", "解释这辆车的行情",
                          context_id="legacy-context", owner_id="owner-a")
    report = {
        "id": "legacy-complete", "question": "解释这辆车的行情", "summaryTitle": "行情分析",
        "summaryText": "中位挂牌价约 12 万元。", "answer": "中位挂牌价约 12 万元。",
        "answerEvidence": {"datasetVersion": "legacy-data", "claims": []},
    }
    connection = repository._connect()
    connection.execute(
        "UPDATE runs SET status='done',result_json=?,updated_at='2026-10-01T00:00:00Z' WHERE run_id=?",
        (json.dumps({"status": "done", "summary": report["answer"], "report": report}), "legacy-complete"),
    )
    connection.execute(
        """INSERT INTO run_events(run_id,seq,event_type,payload_json,created_at)
           VALUES(?,1,'tool_completed',?,?)""",
        ("legacy-complete", json.dumps({
            "event_type": "tool_completed",
            "input": {"tool_call_id": "legacy-call", "tool_name": "price_stats", "arguments": {"private": "drop"}},
            "output": {"ok": True, "note": "旧工具结果", "raw": {"large": "drop"}},
            "status": "done", "duration_ms": 12,
        }), "2026-10-01T00:00:00Z"),
    )
    connection.execute("PRAGMA user_version=7")
    connection.close()

    repository = SQLiteRuntimeRepository(path)
    timeline = repository.list_conversation_entries_owned("legacy-context", "owner-a")["entries"]
    assert [entry["kind"] for entry in timeline] == ["user_message", "execution_group", "assistant_message", "report_card"]
    assert timeline[-1]["payload"]["report_id"] == "legacy-complete"
    assert timeline[1]["payload"]["activities"][0]["tool_name"] == "price_stats"
    assert "arguments" not in str(timeline[1]["payload"]) and "raw" not in str(timeline[1]["payload"])
    artifact = repository.get_report_owned("legacy-complete", "owner-a")
    assert artifact["version"] == 1
    assert artifact["dataset_version"] == "legacy-data"
    assert repository._connect().execute("PRAGMA user_version").fetchone()[0] == 10
    assert list(tmp_path.glob("legacy-v7.sqlite3.pre-v10-*.bak"))

    # Reopening at the current version is safe and does not duplicate entries.
    SQLiteRuntimeRepository(path)
    assert len(repository.list_conversation_entries_owned("legacy-context", "owner-a")["entries"]) == 4


def test_v7_mixed_owner_context_is_preserved_but_not_claimed(tmp_path):
    import sqlite3

    path = tmp_path / "legacy-mixed-owner.sqlite3"
    repository = SQLiteRuntimeRepository(path)
    repository.create_run("owner-a-run", "browser-a", "A 的问题", context_id="shared-context", owner_id="owner-a")
    # Simulate a corrupt/legacy database that normal write APIs correctly refuse.
    connection = repository._connect()
    connection.execute(
        """INSERT INTO runs(run_id,owner_id,session_id,context_id,input_text,context_revision,status,
           next_seq,created_at,updated_at) VALUES('owner-b-run','owner-b','browser-b','shared-context',
           'B 的问题',0,'error',1,'2026-10-01T00:00:00Z','2026-10-01T00:00:00Z')"""
    )
    connection.close()
    connection = repository._connect()
    connection.execute("PRAGMA user_version=9")
    connection.close()

    repository = SQLiteRuntimeRepository(path)
    row = repository._connect().execute(
        "SELECT owner_id FROM sessions WHERE session_id='shared-context'",
    ).fetchone()
    assert row["owner_id"] is None
    with pytest.raises(KeyError):
        repository.get_conversation_owned("shared-context", "owner-a")
    assert repository._connect().execute(
        "SELECT COUNT(*) FROM runs WHERE context_id='shared-context'",
    ).fetchone()[0] == 2


def test_v9_backfill_uses_runtime_snapshot_when_legacy_events_are_missing(tmp_path):
    import json

    path = tmp_path / "legacy-v9-runtime.sqlite3"
    repository = SQLiteRuntimeRepository(path)
    repository.create_run("legacy-runtime", "browser-group", "继续这次分析",
                          context_id="legacy-runtime-context", owner_id="owner-a")
    connection = repository._connect()
    connection.execute(
        """UPDATE runs SET status='done',result_json=?,runtime_json=?,updated_at='2026-10-02T00:00:00Z'
           WHERE run_id=?""",
        (json.dumps({"status": "done", "summary": "历史回答"}), json.dumps({
            "status": "done",
            "plan": [{"id": "verify", "title": "核验结果", "status": "complete", "private": "drop"}],
            "activities": [{"id": "call-legacy", "toolName": "verify_answer", "status": "complete",
                            "detail": "已通过核验", "output": {"ok": True, "raw": "drop"}}],
        }), "legacy-runtime"),
    )
    connection.execute("PRAGMA user_version=9")
    connection.close()

    repository = SQLiteRuntimeRepository(path)
    entries = repository.list_conversation_entries_owned("legacy-runtime-context", "owner-a")["entries"]
    group = next(entry["payload"] for entry in entries if entry["kind"] == "execution_group")
    assert group["plan"][0]["title"] == "核验结果"
    assert group["activities"][0]["tool_name"] == "verify_answer"
    assert group["activities"][0]["output"] == {"ok": True, "note": "已通过核验"}
    assert list(tmp_path.glob("legacy-v9-runtime.sqlite3.pre-v10-*.bak"))


def test_conversation_entries_are_idempotent_and_paginate_by_high_water(tmp_path):
    repository = SQLiteRuntimeRepository(tmp_path / "conversation-entries.sqlite3")
    created = repository.create_conversation("owner-a", "create-entries")
    conversation_id = created["conversation_id"]
    assert conversation_id[0].isalnum()
    first = repository.append_conversation_entry(conversation_id, "user_message", "cmd:1", {"text": "第一问"})
    duplicate = repository.append_conversation_entry(conversation_id, "user_message", "cmd:1", {"text": "第一问"})
    second = repository.append_conversation_entry(conversation_id, "user_message", "cmd:2", {"text": "追问"})
    assert duplicate["entry_id"] == first["entry_id"]
    page = repository.list_conversation_entries_owned(conversation_id, "owner-a", limit=1)
    assert page["high_water_seq"] == 2
    assert page["entries"] == [second]
    older = repository.list_conversation_entries_owned(conversation_id, "owner-a", before_seq=second["conversation_seq"], limit=1)
    assert older["entries"] == [first]
    projected = repository.append_conversation_entry(
        conversation_id, "assistant_message", "assistant:private", {"text": "可见答复", "reasoning": "不得公开"}
    )
    assert projected["payload"] == {"text": "可见答复"}
    with pytest.raises(KeyError):
        repository.list_conversation_entries_owned(conversation_id, "owner-b")


def test_conversation_message_acceptance_is_idempotent_and_creates_one_run_and_entry(tmp_path):
    repository = SQLiteRuntimeRepository(tmp_path / "conversation-message.sqlite3")
    conversation = repository.create_conversation("owner-a", "create-message")
    conversation_id = conversation["conversation_id"]

    accepted = repository.accept_conversation_message(
        conversation_id, owner_id="owner-a", command_id="message-1", content="推荐 15 万内 SUV",
    )
    replay = repository.accept_conversation_message(
        conversation_id, owner_id="owner-a", command_id="message-1", content="推荐 15 万内 SUV",
    )
    assert replay == accepted
    assert accepted["accepted_kind"] == "run"
    assert accepted["entry"]["payload"]["delivery_status"] == "accepted"
    assert repository.get_run_owned(accepted["run_id"], "owner-a")["status"] == "queued"
    assert len(repository.list_runs_owned("owner-a")) == 1
    assert len(repository.list_conversation_entries_owned(conversation_id, "owner-a")["entries"]) == 1

    with pytest.raises(ValueError, match="command_conflict"):
        repository.accept_conversation_message(
            conversation_id, owner_id="owner-a", command_id="message-1", content="改成 20 万",
        )


def test_conversation_message_steers_active_run_and_resumes_waiting_run_atomically(tmp_path):
    repository = SQLiteRuntimeRepository(tmp_path / "conversation-steering.sqlite3")
    conversation_id = repository.create_conversation("owner-a", "create-steering")["conversation_id"]
    repository.create_run("run-active-chat", "browser-group", "原问题", context_id=conversation_id, owner_id="owner-a")
    lease = repository.acquire_run_lease("run-active-chat", "worker", lease_ttl_s=10)
    assert lease

    accepted = repository.accept_conversation_message(
        conversation_id, owner_id="owner-a", command_id="steering-1", content="预算改成 12 万",
    )
    assert accepted["accepted_kind"] == "steering"
    assert accepted["run_id"] == "run-active-chat"
    run = repository.get_run_owned("run-active-chat", "owner-a")
    assert run["runtime"]["input_revision"] == 1
    assert repository.pending_run_messages("run-active-chat", lease_token=lease["token"])[0]["content"] == "预算改成 12 万"

    pending = {
        "tool_call_id": "ask-chat", "questions": [{
            "id": "city", "prompt": "城市？", "options": [], "allow_custom": True,
            "required": True, "selection_mode": "single", "max_selections": 1,
        }],
    }
    checkpoint = {"checkpoint_version": 2, "pending_input": pending, "messages": []}
    repository.pause_run("run-active-chat", checkpoint, pending, lease_token=lease["token"])
    waiting = repository.accept_conversation_message(
        conversation_id, owner_id="owner-a", command_id="steering-2", content="改查杭州",
    )
    assert waiting["accepted_kind"] == "steering"
    assert waiting["status"] == "queued"
    snapshot = repository.get_run_owned("run-active-chat", "owner-a")
    assert snapshot["status"] == "queued"
    assert snapshot["pending_input"] is None
    assert snapshot["request"]["resume_requested"] is True
    entries = repository.list_conversation_entries_owned(conversation_id, "owner-a")["entries"]
    assert [item["payload"]["text"] for item in entries] == ["预算改成 12 万", "改查杭州"]


def test_conversation_message_rejects_report_from_another_context(tmp_path):
    repository = SQLiteRuntimeRepository(tmp_path / "conversation-source-report.sqlite3")
    first = repository.create_conversation("owner-a", "create-report-a")["conversation_id"]
    second = repository.create_conversation("owner-a", "create-report-b")["conversation_id"]
    repository.create_run("run-report-source", "browser-group", "问题", context_id=first, owner_id="owner-a")
    connection = repository._connect()
    try:
        connection.execute(
            """INSERT INTO report_artifacts(report_id,conversation_id,origin_run_id,version,report_schema_version,
               content_json,context_text,evidence_manifest_json,created_at) VALUES(?,?,?,1,1,'{}','','{}','now')""",
            ("report-source", first, "run-report-source"),
        )
    finally:
        connection.close()

    with pytest.raises(ValueError, match="source_report_not_found"):
        repository.accept_conversation_message(
            second, owner_id="owner-a", command_id="source-leak", content="根据这份报告改改",
            source_report_id="report-source",
        )


def test_conversation_message_refuses_steering_after_validated_finalize(tmp_path):
    repository = SQLiteRuntimeRepository(tmp_path / "conversation-finalize-race.sqlite3")
    conversation_id = repository.create_conversation("owner-a", "create-finalize-race")["conversation_id"]
    repository.create_run("run-finalizing", "browser-group", "原问题", context_id=conversation_id, owner_id="owner-a")
    lease = repository.acquire_run_lease("run-finalizing", "worker", lease_ttl_s=10)
    checkpoint = {"checkpoint_version": 2, "flags": {"answer_finalized": True}, "messages": []}
    assert repository.save_run_checkpoint("run-finalizing", checkpoint, lease_token=lease["token"])

    with pytest.raises(ValueError, match="run_finishing"):
        repository.accept_conversation_message(
            conversation_id, owner_id="owner-a", command_id="too-late", content="改一下",
        )
    assert repository.list_conversation_entries_owned(conversation_id, "owner-a")["entries"] == []


def test_repository_protocols_and_transactional_run_claims(tmp_path):
    db_path = tmp_path / "runtime.sqlite3"
    first = SQLiteRuntimeRepository(db_path)
    second = SQLiteRuntimeRepository(db_path)
    assert isinstance(first, RunRepository)
    assert isinstance(first, SessionRepository)
    first.create_run("run-claim", "session-a", "问题", owner_id="owner-a")

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda repository: repository.claim_start("run-claim"), (first, second)))

    assert sorted(claims) == [False, True]
    assert first.get_run("run-claim")["status"] == "running"


def test_state_revision_tracks_semantic_state_not_heartbeat_or_event_watermark(tmp_path):
    repository = SQLiteRuntimeRepository(tmp_path / "state-revision.sqlite3")
    created = repository.create_run("run-state-revision", "session-rev", "question", owner_id="owner-a")
    assert created["state_revision"] == 0
    lease = repository.acquire_run_lease("run-state-revision", "worker", lease_ttl_s=10)
    claimed = repository.get_run("run-state-revision")
    assert claimed["state_revision"] == 1

    assert repository.heartbeat_run("run-state-revision", lease["token"], lease_ttl_s=10)
    event = repository.append_run_event(
        "run-state-revision", {"event_type": "model_round", "round": 1}, lease_token=lease["token"]
    )
    after_telemetry = repository.get_run("run-state-revision")
    assert after_telemetry["state_revision"] == claimed["state_revision"]
    assert after_telemetry["next_seq"] == event["seq"] + 1

    assert repository.save_run_checkpoint(
        "run-state-revision", {"checkpoint_version": 2, "messages": []}, lease_token=lease["token"]
    )
    assert repository.get_run("run-state-revision")["state_revision"] == claimed["state_revision"] + 1


def test_checkpoint_and_public_runtime_projection_commit_together_with_conservative_watermark(tmp_path):
    repository = SQLiteRuntimeRepository(tmp_path / "runtime-projection.sqlite3")
    repository.create_run("run-runtime-projection", "session-projection", "question", owner_id="owner-a")
    lease = repository.acquire_run_lease("run-runtime-projection", "worker", lease_ttl_s=10)
    first = repository.append_run_event(
        "run-runtime-projection", {"event_type": "model_round", "round": 1}, lease_token=lease["token"]
    )
    checkpoint = {
        "checkpoint_version": 4,
        "messages": [{"role": "assistant", "content": "private conversation"}],
        "tool_results": [{"output": {"private": "result"}}],
        "flags": {"plan_revision": 2, "input_revision": 1},
        "task_intent": {
            "revision": 3,
            "mode": "planned",
            "needs_plan": True,
            "goals": [{"id": "g1", "description": "查车并整理", "deliverable": "结果", "depends_on": []}],
        },
        "plan": [
            {"id": "query", "title": "查询", "description": "取数", "tool_names": ["recommend"],
             "goal_ids": ["g1"], "kind": "data", "completion_criteria": "tool_result", "status": "running"},
        ],
        "run_logs": [],
        "pending_input": None,
    }

    assert repository.save_run_checkpoint("run-runtime-projection", checkpoint, lease_token=lease["token"])
    snapshot = repository.get_run("run-runtime-projection")
    assert snapshot["runtime"]["plan"] == checkpoint["plan"]
    assert snapshot["runtime"]["task_revision"] == 3
    assert snapshot["covered_event_seq"] == first["seq"]
    assert snapshot["runtime"]["state_revision"] == snapshot["state_revision"]

    later = repository.append_run_event(
        "run-runtime-projection",
        {"event_type": "plan_update", "output": {"steps": checkpoint["plan"], "revision": 2}},
        lease_token=lease["token"],
    )
    after_event = repository.get_run("run-runtime-projection")
    assert after_event["next_seq"] - 1 == later["seq"]
    assert after_event["covered_event_seq"] == first["seq"]
    assert later["run_id"] == "run-runtime-projection"
    assert later["attempt"] == lease["attempt"]
    assert later["event_seq"] == later["seq"]

    public = public_run_snapshot(after_event)
    assert public["runtime"]["plan"][0]["id"] == "query"
    assert public["runtime"]["task_revision"] == 3
    assert public["covered_event_seq"] == first["seq"]
    assert "private conversation" not in str(public)
    assert "tool_results" not in str(public)


def test_sqlite_run_snapshot_omits_event_history_and_event_reads_are_bounded(tmp_path):
    repository = SQLiteRuntimeRepository(tmp_path / "bounded-events.sqlite3")
    repository.create_run("run-bounded-events", "session-events", "question", owner_id="owner-a")
    lease = repository.acquire_run_lease("run-bounded-events", "worker", lease_ttl_s=10)
    for index in range(35):
        repository.append_run_event(
            "run-bounded-events", {"event_type": "model_round", "round": index}, lease_token=lease["token"]
        )

    snapshot = repository.get_run("run-bounded-events")
    first_page = repository.read_run_events("run-bounded-events", 0, limit=10)
    second_page = repository.read_run_events("run-bounded-events", 10, limit=10)
    assert "events" not in snapshot
    assert snapshot["next_seq"] == 36
    assert len(first_page) == len(second_page) == 10
    assert first_page[0]["seq"] == 1 and first_page[-1]["seq"] == 10
    assert second_page[0]["seq"] == 11 and second_page[-1]["seq"] == 20


def test_run_store_waiting_and_completion_contracts_reject_invalid_values(tmp_path):
    repository = SQLiteRuntimeRepository(tmp_path / "invalid-contract.sqlite3")
    repository.create_run("run-invalid-contract", "session-invalid", "question", owner_id="owner-a")
    lease = repository.acquire_run_lease("run-invalid-contract", "worker", lease_ttl_s=10)

    with pytest.raises(ValueError, match="waiting_outcome_missing_tool_call_id"):
        repository.pause_run(
            "run-invalid-contract", {"checkpoint_version": 2}, {"questions": [{"id": "budget"}]},
            lease_token=lease["token"],
        )
    with pytest.raises(ValueError, match="completed_outcome_missing_result"):
        repository.complete_run("run-invalid-contract", None, lease_token=lease["token"])
    assert repository.get_run("run-invalid-contract")["status"] == "running"


def test_accepting_a_run_claims_an_empty_context_for_its_owner(tmp_path):
    repository = SQLiteRuntimeRepository(tmp_path / "accept-context.sqlite3")
    created = repository.create_run(
        "run-context-accepted", "history-owner", "问题",
        context_id="context-owner", owner_id="owner-a",
    )

    context = repository.load_session_owned("context-owner", "owner-a")
    assert created["context_id"] == "context-owner"
    assert context.messages == []
    assert context.revision == 0
    with pytest.raises(KeyError, match="会话不存在"):
        repository.create_run(
            "run-context-hijack", "history-other", "问题",
            context_id="context-owner", owner_id="owner-b",
        )
    with pytest.raises(KeyError):
        repository.get_run("run-context-hijack")


def test_client_request_id_is_owner_scoped_and_payload_bound(tmp_path):
    repository = SQLiteRuntimeRepository(tmp_path / "idempotency.sqlite3")
    first = repository.create_run(
        "run-idempotent-a", "session-idem", "question", owner_id="owner-a",
        request_data={"confirmed_filters": {"city": "北京"}}, client_request_id="submit-1",
    )
    replay = repository.create_run(
        "run-idempotent-b", "session-idem", "question", owner_id="owner-a",
        request_data={"confirmed_filters": {"city": "北京"}}, client_request_id="submit-1",
    )
    assert replay["run_id"] == first["run_id"]
    with pytest.raises(ValueError, match="client_request_conflict"):
        repository.create_run(
            "run-idempotent-c", "session-idem", "different", owner_id="owner-a",
            request_data={"confirmed_filters": {"city": "北京"}}, client_request_id="submit-1",
        )
    other_owner = repository.create_run(
        "run-idempotent-d", "session-idem-b", "question", owner_id="owner-b",
        request_data={"confirmed_filters": {"city": "北京"}}, client_request_id="submit-1",
    )
    assert other_owner["run_id"] != first["run_id"]


def test_resume_client_request_replays_once_and_rejects_changed_payload(tmp_path):
    repository = SQLiteRuntimeRepository(tmp_path / "resume-idempotency.sqlite3")
    repository.create_run("run-input-idem", "session-input", "question", owner_id="owner-a")
    lease = repository.acquire_run_lease("run-input-idem", "worker", lease_ttl_s=10)
    pending = {"tool_call_id": "ask-1", "questions": [{"id": "budget"}]}
    repository.pause_run(
        "run-input-idem", {"checkpoint_version": 2, "pending_input": pending}, pending,
        lease_token=lease["token"],
    )
    request_hash = canonical_request_hash({"run_id": "run-input-idem", "answers": [{"question_id": "budget", "values": ["15"]}]})
    accepted = repository.prepare_run_resume(
        "run-input-idem", {"resume_requested": True}, expected_tool_call_id="ask-1", expected_revision=0,
        client_request_id="answer-1", request_hash=request_hash,
    )
    replay = repository.prepare_run_resume(
        "run-input-idem", {"resume_requested": True}, expected_tool_call_id="ask-1", expected_revision=0,
        client_request_id="answer-1", request_hash=request_hash,
    )
    assert accepted is replay is True
    saved = repository.get_client_request("owner-a", "input", "answer-1", request_hash)
    assert saved["response"] == {"accepted_tool_call_id": "ask-1", "resume_revision": 1}
    with pytest.raises(ValueError, match="client_request_conflict"):
        repository.prepare_run_resume(
            "run-input-idem", {"resume_requested": True}, expected_tool_call_id="ask-1", expected_revision=0,
            client_request_id="answer-1", request_hash="different-payload",
        )


def test_session_append_with_a_run_lease_rejects_stale_workers(tmp_path):
    repository = SQLiteRuntimeRepository(tmp_path / "fenced-session.sqlite3")
    repository.create_run(
        "run-session-fenced", "history-fenced", "問題",
        context_id="context-fenced", owner_id="owner-a",
    )
    lease = repository.acquire_run_lease("run-session-fenced", "worker", lease_ttl_s=10)
    assert repository.append_session_messages(
        "context-fenced",
        [{"role": "user", "content": "accepted"}],
        run_id="run-session-fenced",
        owner_id="owner-a",
        lease_token=lease["token"],
    ).revision == 1

    repository.finish_run("run-session-fenced", {"summary": "done"}, lease_token=lease["token"])
    with pytest.raises(RuntimeError, match="run_lease_conflict"):
        repository.append_session_messages(
            "context-fenced",
            [{"role": "assistant", "content": "late write"}],
            run_id="run-session-fenced",
            owner_id="owner-a",
            lease_token=lease["token"],
        )
    assert [message["content"] for message in repository.load_session_owned("context-fenced", "owner-a").messages] == ["accepted"]


def test_complete_run_commits_session_result_and_terminal_event_atomically(tmp_path, monkeypatch):
    repository = SQLiteRuntimeRepository(tmp_path / "complete-atomic.sqlite3")
    repository.create_run(
        "run-complete-atomic", "history-complete", "question",
        context_id="context-complete", owner_id="owner-a",
    )
    lease = repository.acquire_run_lease("run-complete-atomic", "worker", lease_ttl_s=10)
    session_delta = [
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "answer"},
    ]

    def fail_after_event_insert():
        raise RuntimeError("injected_event_write_failure")

    monkeypatch.setattr(repository, "_after_event_insert", fail_after_event_insert)
    with pytest.raises(RuntimeError, match="injected_event_write_failure"):
        repository.complete_run(
            "run-complete-atomic",
            {"status": "done", "summary": "answer"},
            session_messages=session_delta,
            session_run_id="run-complete-atomic",
            lease_token=lease["token"],
        )
    assert repository.get_run("run-complete-atomic")["status"] == "running"
    assert repository.load_session_owned("context-complete", "owner-a").revision == 0
    assert repository.read_run_events("run-complete-atomic") == []

    monkeypatch.setattr(repository, "_after_event_insert", lambda: None)
    completed = repository.complete_run(
        "run-complete-atomic",
        {"status": "done", "summary": "answer"},
        session_messages=session_delta,
        session_run_id="run-complete-atomic",
        lease_token=lease["token"],
    )
    duplicate = repository.complete_run(
        "run-complete-atomic",
        {"status": "done", "summary": "duplicate"},
        session_messages=session_delta,
        session_run_id="run-complete-atomic",
        lease_token=lease["token"],
    )

    assert completed["status"] == "done"
    assert duplicate["result"]["summary"] == "answer"
    assert [event["event_type"] for event in repository.read_run_events("run-complete-atomic")] == ["done"]
    assert [message["content"] for message in repository.load_session_owned("context-complete", "owner-a").messages] == ["question", "answer"]


def test_complete_run_persists_redacted_historical_execution_group_once(tmp_path):
    repository = SQLiteRuntimeRepository(tmp_path / "execution-group.sqlite3")
    repository.create_run("run-execution-group", "browser-group", "question", context_id="context-execution", owner_id="owner-a")
    lease = repository.acquire_run_lease("run-execution-group", "worker", lease_ttl_s=10)
    repository.append_run_event("run-execution-group", {
        "event_type": "plan_update",
        "output": {"steps": [{"id": "price", "title": "查询价格", "status": "complete", "private": "drop"}]},
        "reasoning_content": "hidden reasoning must not be stored in the timeline",
    }, lease_token=lease["token"])
    repository.append_run_event("run-execution-group", {
        "event_type": "tool_started", "input": {"tool_call_id": "call-price", "tool_name": "price_stats", "plan_step_id": "price", "arguments": {"secret": "do not copy"}},
    }, lease_token=lease["token"])
    repository.append_run_event("run-execution-group", {
        "event_type": "tool_completed", "input": {"tool_call_id": "call-price", "tool_name": "price_stats", "plan_step_id": "price", "arguments": {"secret": "do not copy"}},
        "output": {"ok": True, "note": "已取得 261 条样本", "raw": {"large": "do not copy"}},
        "status": "done", "duration_ms": 18,
    }, lease_token=lease["token"])

    result = repository.complete_run(
        "run-execution-group", {"status": "done", "summary": "已校验答案"},
        session_messages=[{"role": "user", "content": "question"}, {"role": "assistant", "content": "已校验答案"}],
        session_run_id="run-execution-group", lease_token=lease["token"],
    )
    duplicate = repository.complete_run(
        "run-execution-group", {"status": "done", "summary": "duplicate"},
        session_messages=[], session_run_id="run-execution-group", lease_token=lease["token"],
    )

    assert duplicate["result"]["entry_ids"] == result["result"]["entry_ids"]
    entries = repository.list_conversation_entries_owned("context-execution", "owner-a")["entries"]
    assert [entry["kind"] for entry in entries] == ["execution_group", "assistant_message"]
    group = entries[0]["payload"]
    assert group["status"] == "complete"
    assert group["plan"][0]["title"] == "查询价格"
    assert group["activities"][0]["tool_name"] == "price_stats"
    assert group["activities"][0]["output"] == {"ok": True, "note": "已取得 261 条样本"}
    assert "arguments" not in str(group) and "reasoning_content" not in str(group)
    assert repository.read_run_events("run-execution-group")[-1]["event_type"] == "done"


def test_complete_run_does_not_fail_when_optional_execution_event_is_corrupt(tmp_path):
    repository = SQLiteRuntimeRepository(tmp_path / "corrupt-execution-event.sqlite3")
    repository.create_run("run-corrupt-event", "browser-group", "question", context_id="context-corrupt-event", owner_id="owner-a")
    lease = repository.acquire_run_lease("run-corrupt-event", "worker", lease_ttl_s=10)
    connection = repository._connect()
    connection.execute(
        "INSERT INTO run_events(run_id,seq,event_type,payload_json,created_at) VALUES(?,1,'tool_completed','not-json','now')",
        ("run-corrupt-event",),
    )
    connection.execute("UPDATE runs SET next_seq=2 WHERE run_id='run-corrupt-event'")
    connection.close()

    completed = repository.complete_run(
        "run-corrupt-event", {"status": "done", "summary": "validated answer"},
        lease_token=lease["token"],
    )

    assert completed["status"] == "done"
    assert completed["result"]["summary"] == "validated answer"
    entries = repository.list_conversation_entries_owned("context-corrupt-event", "owner-a")["entries"]
    assert [entry["kind"] for entry in entries] == ["assistant_message"]


def test_accepted_steering_wins_completion_race_and_requeues_last_checkpoint(tmp_path):
    repository = SQLiteRuntimeRepository(tmp_path / "steering-completion-race.sqlite3")
    conversation = repository.create_conversation("owner-a", "create-race")
    conversation_id = conversation["conversation_id"]
    accepted_run = repository.accept_conversation_message(
        conversation_id, owner_id="owner-a", command_id="initial-message", content="最初的问题",
    )
    run_id = accepted_run["run_id"]
    lease = repository.acquire_run_lease(run_id, "worker-a", lease_ttl_s=10)
    checkpoint = {
        "messages": [{"role": "user", "content": "最初的问题"}],
        "flags": {"steps": 2}, "run_logs": [], "plan": [],
    }
    assert repository.save_run_checkpoint(run_id, checkpoint, lease_token=lease["token"])
    steering = repository.accept_conversation_message(
        conversation_id, owner_id="owner-a", command_id="steering-race",
        content="补充：预算改成 18 万",
    )
    assert steering["accepted_kind"] == "steering"

    stale_result = {
        "status": "done", "summary": "旧条件下的答案",
        "final_message": {"role": "assistant", "content": "旧条件下的答案"},
        "report": {"id": run_id, "summaryTitle": "旧答案", "summaryText": "旧条件"},
    }
    result = repository.complete_run(
        run_id, stale_result,
        session_messages=[{"role": "user", "content": "最初的问题"},
                          {"role": "assistant", "content": "旧条件下的答案"}],
        session_run_id=run_id, lease_token=lease["token"],
    )
    assert result["status"] == "queued"
    assert result["result"] is None
    assert result["checkpoint"] == checkpoint
    assert repository.load_session_owned(conversation_id, "owner-a").revision == 0
    assert [entry["kind"] for entry in repository.list_conversation_entries_owned(conversation_id, "owner-a")["entries"]] == ["user_message", "user_message"]
    with pytest.raises(KeyError):
        repository.get_report_owned(run_id, "owner-a")

    retry_lease = repository.acquire_run_lease(run_id, "worker-b", lease_ttl_s=10)
    pending = repository.pending_run_messages(run_id, lease_token=retry_lease["token"])
    assert [message["content"] for message in pending] == ["补充：预算改成 18 万"]
    assert repository.apply_run_messages(
        run_id, pending, checkpoint, lease_token=retry_lease["token"],
    )


def test_success_report_artifact_entries_and_done_references_commit_once(tmp_path, monkeypatch):
    repository = SQLiteRuntimeRepository(tmp_path / "complete-report-atomic.sqlite3")
    repository.create_run("run-report-atomic", "browser-group", "原问题", context_id="context-report", owner_id="owner-a")
    lease = repository.acquire_run_lease("run-report-atomic", "worker", lease_ttl_s=10)
    result = {
        "status": "done", "summary": "已校验答案", "summary_source": "validated",
        "context_id": "context-report",
        "final_message": {"role": "assistant", "content": "已校验答案", "source": "validated"},
        "report": {"id": "run-report-atomic", "summaryTitle": "分析报告", "summaryText": "报告摘要",
                   "answer": "已校验答案", "reliability": {"sampleSize": 3, "confidence": "medium"},
                   "answerEvidence": {"datasetVersion": "dataset-v1", "items": []}},
    }
    messages = [{"role": "user", "content": "原问题"}]

    assert repository.complete_run("run-report-atomic", result, session_messages=messages,
                                   session_run_id="run-report-atomic", lease_token="wrong-lease") is None
    assert repository.get_run("run-report-atomic")["status"] == "running"
    assert repository.list_conversation_entries_owned("context-report", "owner-a")["entries"] == []

    def fail_event():
        raise RuntimeError("event_write_failed")

    monkeypatch.setattr(repository, "_after_event_insert", fail_event)
    with pytest.raises(RuntimeError, match="event_write_failed"):
        repository.complete_run("run-report-atomic", result, session_messages=messages,
                                session_run_id="run-report-atomic", lease_token=lease["token"])
    assert repository.get_run("run-report-atomic")["status"] == "running"
    assert repository.list_conversation_entries_owned("context-report", "owner-a")["entries"] == []
    with pytest.raises(KeyError):
        repository.get_report_owned("run-report-atomic", "owner-a")

    monkeypatch.setattr(repository, "_after_event_insert", lambda: None)
    completed = repository.complete_run("run-report-atomic", result, session_messages=messages,
                                        session_run_id="run-report-atomic", lease_token=lease["token"])
    duplicate = repository.complete_run("run-report-atomic", result, session_messages=messages,
                                        session_run_id="run-report-atomic", lease_token=lease["token"])
    assert completed["result"]["report_ref"]["version"] == 1
    assert duplicate["result"]["entry_ids"] == completed["result"]["entry_ids"]
    entries = repository.list_conversation_entries_owned("context-report", "owner-a")["entries"]
    assert [entry["kind"] for entry in entries] == ["assistant_message", "report_card"]
    assert entries[1]["payload"]["report_id"] == "run-report-atomic"
    assert len(repository.read_run_events("run-report-atomic")) == 1
    assert repository.read_run_events("run-report-atomic")[0]["report_ref"]["report_id"] == "run-report-atomic"
    transcript = repository.load_session_owned("context-report", "owner-a").messages
    assert [message["content"] for message in transcript] == ["原问题", "已校验答案"]
    artifact = repository.get_report_owned("run-report-atomic", "owner-a")
    assert artifact["content"]["summaryText"] == "报告摘要"
    assert "validated_answer: 已校验答案" in artifact["context_text"]


def test_pause_run_commits_session_checkpoint_and_question_atomically(tmp_path, monkeypatch):
    repository = SQLiteRuntimeRepository(tmp_path / "pause-atomic.sqlite3")
    repository.create_run(
        "run-pause-atomic", "history-pause", "question",
        context_id="context-pause", owner_id="owner-a",
    )
    lease = repository.acquire_run_lease("run-pause-atomic", "worker", lease_ttl_s=10)
    pending = {"tool_call_id": "ask-1", "questions": [{"id": "purpose"}]}
    checkpoint = {"checkpoint_version": 4, "messages": [{"role": "user", "content": "question"}],
                  "pending_input": pending, "flags": {"session_message_cursor": 1}}
    delta = [{"role": "user", "content": "question"}, {"role": "assistant", "content": "Need a detail."}]
    kwargs = {
        "waiting_event": {"event_type": "input_required", "pending_input": pending},
        "session_messages": delta,
        "session_run_id": "run-pause-atomic-attempt-1",
    }

    def fail_after_event_insert():
        raise RuntimeError("injected_pause_event_failure")

    monkeypatch.setattr(repository, "_after_event_insert", fail_after_event_insert)
    with pytest.raises(RuntimeError, match="injected_pause_event_failure"):
        repository.pause_run(
            "run-pause-atomic", checkpoint, pending, result={"status": "waiting_user"},
            lease_token=lease["token"], **kwargs,
        )
    assert repository.get_run("run-pause-atomic")["status"] == "running"
    assert repository.load_session_owned("context-pause", "owner-a").revision == 0
    assert repository.read_run_events("run-pause-atomic") == []

    monkeypatch.setattr(repository, "_after_event_insert", lambda: None)
    before = repository.get_run("run-pause-atomic")
    assert repository.pause_run(
        "run-pause-atomic", checkpoint, pending, result={"status": "waiting_user"},
        lease_token="stale-token", **kwargs,
    ) is None
    assert repository.get_run("run-pause-atomic")["version"] == before["version"]
    assert repository.load_session_owned("context-pause", "owner-a").revision == 0
    assert repository.read_run_events("run-pause-atomic") == []

    paused = repository.pause_run(
        "run-pause-atomic", checkpoint, pending, result={"status": "waiting_user"},
        lease_token=lease["token"], **kwargs,
    )
    replay = repository.pause_run(
        "run-pause-atomic", checkpoint, pending, result={"status": "waiting_user"},
        lease_token="stale-token", **kwargs,
    )
    session = repository.load_session_owned("context-pause", "owner-a")
    assert paused["status"] == replay["status"] == "waiting_user"
    assert paused["context_revision"] == 1
    assert session.revision == 1
    assert [message["content"] for message in session.messages] == ["question", "Need a detail."]
    assert [event["event_type"] for event in repository.read_run_events("run-pause-atomic")] == ["input_required"]


def test_worker_lost_checkpoint_can_be_recovered_once_with_owner_and_version_cas(tmp_path):
    repository = SQLiteRuntimeRepository(tmp_path / "recover.sqlite3")
    repository.create_run("run-recover", "session-r", "问题", owner_id="owner-a")
    lease = repository.acquire_run_lease("run-recover", "worker", lease_ttl_s=10)
    checkpoint = {
        "checkpoint_version": 2,
        "messages": [{"role": "system", "content": "s"}, {"role": "user", "content": "问题"}],
        "pending_input": None,
        "pending_tool_batch": None,
    }
    assert repository.save_run_checkpoint("run-recover", checkpoint, lease_token=lease["token"])
    # Simulate a dead worker whose lease has expired; recovery must reuse the
    # same logical run and the last durably saved safe boundary.
    repository.reconcile_runs(now_iso="2999-01-01T00:00:00+00:00")
    lost = repository.get_run("run-recover")
    assert lost["failure_code"] == "worker_lost"
    assert lost["checkpoint"] == checkpoint
    assert repository.recover_run("run-recover", owner_id="other", expected_version=lost["version"]) is None
    recovery_hash = canonical_request_hash({
        "operation": "recover", "run_id": "run-recover", "expected_version": lost["version"],
    })
    recovered = repository.recover_run(
        "run-recover", owner_id="owner-a", expected_version=lost["version"],
        client_request_id="recover-once", request_hash=recovery_hash,
    )
    assert recovered["status"] == "queued"
    assert recovered["run_id"] == "run-recover"
    assert recovered["checkpoint"] == checkpoint
    replay = repository.recover_run(
        "run-recover", owner_id="owner-a", expected_version=lost["version"],
        client_request_id="recover-once", request_hash=recovery_hash,
    )
    assert replay["status"] == "queued"
    with pytest.raises(ValueError, match="client_request_conflict"):
        repository.recover_run(
            "run-recover", owner_id="owner-a", expected_version=lost["version"] + 1,
            client_request_id="recover-once", request_hash="different-payload",
        )
    assert repository.recover_run(
        "run-recover", owner_id="owner-a", expected_version=lost["version"]
    ) is None


def test_steering_messages_are_revisioned_idempotent_and_public_events_omit_content(tmp_path):
    repository = SQLiteRuntimeRepository(tmp_path / "messages.sqlite3")
    repository.create_run("run-message", "session-m", "question", owner_id="owner-a")
    lease = repository.acquire_run_lease("run-message", "worker", lease_ttl_s=10)
    accepted = repository.accept_run_message(
        "run-message", owner_id="owner-a", client_message_id="client-1",
        content="change city to Beijing", expected_revision=0,
    )
    assert accepted == {"status": "accepted", "client_message_id": "client-1", "revision": 1, "idempotent": False}
    duplicate = repository.accept_run_message(
        "run-message", owner_id="owner-a", client_message_id="client-1",
        content="change city to Beijing", expected_revision=0,
    )
    assert duplicate["idempotent"] is True
    assert repository.accept_run_message(
        "run-message", owner_id="owner-a", client_message_id="client-2",
        content="stale", expected_revision=0,
    ) is None
    queued = repository.pending_run_messages("run-message", lease_token=lease["token"])
    assert queued[0]["content"] == "change city to Beijing"
    assert repository.apply_run_messages("run-message", queued, {"checkpoint_version": 2}, lease_token=lease["token"])
    assert repository.pending_run_messages("run-message", lease_token=lease["token"]) == []
    events = repository.read_run_events("run-message")
    assert [event["event_type"] for event in events] == ["message_received", "message_applied"]
    assert all("content" not in event for event in events)


def test_waiting_user_message_atomically_invalidates_question_and_requeues(tmp_path):
    repository = SQLiteRuntimeRepository(tmp_path / "waiting-message.sqlite3")
    repository.create_run("run-waiting-message", "session-wm", "question", owner_id="owner-a")
    lease = repository.acquire_run_lease("run-waiting-message", "worker", lease_ttl_s=10)
    checkpoint = {"checkpoint_version": 2, "messages": [], "pending_input": {
        "tool_call_id": "ask-1", "questions": [{"id": "budget"}],
    }, "pending_tool_batch": {"next_index": 0, "calls": []}}
    paused = repository.pause_run(
        "run-waiting-message", checkpoint, checkpoint["pending_input"],
        runtime={"input_revision": 0}, lease_token=lease["token"],
    )
    assert paused["status"] == "waiting_user"
    accepted = repository.accept_run_message(
        "run-waiting-message", owner_id="owner-a", client_message_id="steer-1",
        content="budget is 15", expected_revision=0,
    )
    assert accepted["status"] == "queued"
    waiting = repository.get_run("run-waiting-message")
    assert waiting["status"] == "queued"
    assert waiting["pending_input"] is None
    assert waiting["request"]["steering_client_message_id"] == "steer-1"


def test_database_enables_wal_foreign_keys_and_busy_timeout_per_connection(tmp_path):
    repository = SQLiteRuntimeRepository(tmp_path / "runtime.sqlite3", busy_timeout_ms=1234)
    connection = repository._connect()
    try:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 1234
    finally:
        connection.close()


def test_cross_instance_event_sequence_and_session_append_are_serialized(tmp_path):
    db_path = tmp_path / "runtime.sqlite3"
    repositories = [SQLiteRuntimeRepository(db_path), SQLiteRuntimeRepository(db_path)]
    repositories[0].create_run("run-events", "session-events", "问题", owner_id="owner-a")
    repositories[0].claim_start("run-events")

    def append_event(index):
        repositories[index % 2].append_run_event("run-events", {"event_type": "model_round", "step": index})

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(append_event, range(32)))
    events = repositories[1].read_run_events("run-events")
    assert [event["seq"] for event in events] == list(range(1, 33))

    def append_message(index):
        repositories[index % 2].append_session_messages(
            "session-shared",
            [{"role": "user", "content": f"问题 {index}"}],
            owner_id="owner-a",
            run_id=f"run-message-{index}",
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(append_message, range(24)))
    session = repositories[0].load_session_owned("session-shared", "owner-a")
    assert len(session.messages) == 24
    assert session.revision == 24
    assert len(repositories[1].active_session_records("session-shared")) == 24


def test_session_rollback_revision_cas_allows_only_one_winner(tmp_path):
    repository = SQLiteRuntimeRepository(tmp_path / "runtime.sqlite3")
    snapshot = repository.append_session_messages(
        "session-cas",
        [{"role": "user", "content": "第一条"}, {"role": "user", "content": "第二条"}],
        owner_id="owner-a",
    )
    target_id = snapshot.messages[0]["message_id"]

    def rollback():
        try:
            repository.rollback_session("session-cas", target_id, expected_revision=1, owner_id="owner-a")
            return "ok"
        except ValueError:
            return "stale"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: rollback(), range(2)))

    assert sorted(results) == ["ok", "stale"]
    assert repository.load_session("session-cas").revision == 2


def test_event_insert_and_run_update_roll_back_as_one_transaction(tmp_path, monkeypatch):
    repository = SQLiteRuntimeRepository(tmp_path / "runtime.sqlite3")
    repository.create_run("run-atomic", "session-atomic", "问题", owner_id="owner-a")
    before = repository.get_run("run-atomic")

    def fail_after_event_insert():
        raise RuntimeError("injected transaction failure")

    monkeypatch.setattr(repository, "_after_event_insert", fail_after_event_insert)
    with pytest.raises(RuntimeError, match="injected"):
        repository.append_run_event("run-atomic", {"event_type": "started"})

    after = repository.get_run("run-atomic")
    assert after["next_seq"] == before["next_seq"]
    assert repository.read_run_events("run-atomic") == []


def test_sqlite_failure_classification_distinguishes_busy_locked_and_permanent_errors():
    import sqlite3
    from backend.agent_runtime.sqlite_repository import classify_sqlite_failure
    for code, name, retryable in [
        (sqlite3.SQLITE_BUSY | (2 << 8), "storage_busy", True),
        (sqlite3.SQLITE_LOCKED, "storage_locked", True),
        (sqlite3.SQLITE_READONLY, "storage_readonly", False),
        (sqlite3.SQLITE_CORRUPT, "storage_corrupt", False),
    ]:
        error = sqlite3.OperationalError("sensitive details ignored")
        error.sqlite_errorcode = code
        assert classify_sqlite_failure(error) == (name, retryable)
    assert classify_sqlite_failure(RuntimeError("database is locked")) is None
