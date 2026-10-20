import json
import threading
import time
import pytest

from fastapi.testclient import TestClient

from backend.agent_runtime.run_store import RunStore
from backend.agent_runtime.session_store import SessionStore
from backend.agent_runtime.context_budget import ContextBudget
from backend.agent_runtime.sqlite_repository import SQLiteRunStore, SQLiteRuntimeRepository, SQLiteSessionStore
from backend.agent_runtime.state import AgentState, CheckpointMigrationRequired
from backend.agent_runtime.scheduler import SubmitResult
from backend.agent_runtime.nodes import append_user_steering
from backend.agent_runtime.message_protocol import validate_message_protocol
from backend.agent_runtime.task_context import initial_task_context
from backend.agent_runtime.loop import _current_evidence
from backend.auth import owner_id_from_cookie
from backend.service import create_app


def test_capabilities_endpoint_returns_the_current_shared_data_contract(monkeypatch):
    import backend.service as service_module
    expected = {
        "dataset_version": "data-version-1",
        "row_count": 42,
        "fields": {},
        "energy_values": ["电动"],
        "supported_filters": ["budget"],
        "unsupported_filters": ["body_type"],
        "data_time_range": {"status": "unknown", "start": None, "end": None},
    }
    monkeypatch.setattr(service_module, "get_data_capabilities", lambda: expected)

    response = TestClient(create_app(run_store=RunStore(), session_store=SessionStore())).get("/v1/capabilities")

    assert response.status_code == 200
    assert response.json() == expected


def test_http_client_request_id_replays_original_run_and_rejects_payload_change(tmp_path):
    repository = SQLiteRuntimeRepository(tmp_path / "submit-idempotency.sqlite3")
    calls = []

    def fake_runner(**kwargs):
        calls.append(kwargs["run_id"])
        return {"status": "done", "summary": "固定脚本结果"}

    client = TestClient(create_app(runtime_repository=repository, runner=fake_runner))
    payload = {"input": "查询宝马", "filters": {}, "client_request_id": "client-submit-1"}
    first = client.post("/v1/runs", json=payload)
    replay = client.post("/v1/runs", json=payload)

    assert first.status_code == replay.status_code == 202
    assert replay.json()["run_id"] == first.json()["run_id"]
    conflict = client.post("/v1/runs", json={**payload, "input": "查询奥迪"})
    assert conflict.status_code == 409
    for _ in range(100):
        if client.get(f"/v1/runs/{first.json()['run_id']}").json()["status"] == "done":
            break
        time.sleep(0.01)
    assert repository.get_run(first.json()["run_id"])["status"] == "done"
    assert calls == [first.json()["run_id"]]


def test_http_completion_publishes_atomic_report_and_timeline_refs(tmp_path):
    repository = SQLiteRuntimeRepository(tmp_path / "http-report-atomic.sqlite3")

    def fake_runner(**kwargs):
        run_id = kwargs["run_id"]
        answer = "经过校验的最终答复"
        return {
            "status": "done", "summary": answer, "summary_source": "validated",
            "context_id": kwargs["context_id"],
            "final_message": {"role": "assistant", "content": answer, "source": "validated"},
            "report": {"id": run_id, "summaryTitle": "测试报告", "summaryText": answer, "answer": answer,
                       "reliability": {"sampleSize": 2, "confidence": "medium"},
                       "answerEvidence": {"datasetVersion": "test-v1", "items": []}},
            "_session_commit": {"messages": [{"role": "user", "content": kwargs["user_text"]}], "run_id": run_id},
        }

    client = TestClient(create_app(runtime_repository=repository, runner=fake_runner))
    submitted = client.post("/v1/runs", json={"run_id": "run-http-report", "input": "测试问题"})
    assert submitted.status_code == 202
    for _ in range(100):
        snapshot = client.get("/v1/runs/run-http-report").json()
        if snapshot["status"] in {"done", "error"}:
            break
        time.sleep(0.01)
    assert snapshot["status"] == "done"
    assert snapshot["result"]["report_ref"]["report_id"] == "run-http-report"
    assert {"assistant_entry_id", "report_entry_id"} <= set(snapshot["result"]["entry_ids"])
    assert set(snapshot["result"]["entry_ids"]) <= {"execution_group_entry_id", "assistant_entry_id", "report_entry_id"}
    done = repository.read_run_events("run-http-report")[-1]
    assert done["event_type"] == "done"
    assert done["report_ref"]["version"] == 1
    assert client.get("/v1/reports/run-http-report").json()["content"]["summaryTitle"] == "测试报告"
    assert TestClient(client.app).get("/v1/reports/run-http-report").status_code == 404
    owner_id = repository.get_run("run-http-report")["owner_id"]
    entries = repository.list_conversation_entries_owned(snapshot["context_id"], owner_id)
    kinds = [item["kind"] for item in entries["entries"]]
    assert kinds[-2:] == ["assistant_message", "report_card"]
    assert kinds[:-2] in ([], ["execution_group"])
    assert [item["content"] for item in repository.load_session_owned(snapshot["context_id"], owner_id).messages] == [
        "测试问题", "经过校验的最终答复",
    ]


def test_checkpoint_migration_failure_is_explicit_and_not_retryable(tmp_path):
    repository = SQLiteRuntimeRepository(tmp_path / "checkpoint-migration.sqlite3")

    def fake_runner(**_kwargs):
        raise CheckpointMigrationRequired("旧 checkpoint 无法安全迁移")

    client = TestClient(create_app(runtime_repository=repository, runner=fake_runner))
    created = client.post("/v1/runs", json={"input": "继续旧分析"})
    assert created.status_code == 202
    run_id = created.json()["run_id"]
    for _ in range(100):
        snapshot = client.get(f"/v1/runs/{run_id}").json()
        if snapshot["status"] == "error":
            break
        time.sleep(0.01)

    assert snapshot["status"] == "error"
    assert snapshot["failure_code"] == "migration_required"
    assert snapshot["retryable"] is False


def test_non_retryable_failure_cannot_be_retried_via_http(tmp_path):
    repository = SQLiteRuntimeRepository(tmp_path / "non-retryable.sqlite3")

    def fake_runner(**_kwargs):
        return {"status": "error", "summary": "答案未通过校验。", "failure_code": "answer_invalid", "retryable": False}

    client = TestClient(create_app(runtime_repository=repository, runner=fake_runner))
    created = client.post("/v1/runs", json={"input": "查询挂牌价"})
    run_id = created.json()["run_id"]
    for _ in range(100):
        if repository.get_run(run_id)["status"] == "error":
            break
        time.sleep(0.01)

    retried = client.post(f"/v1/runs/{run_id}/retry", json={})
    assert retried.status_code == 409
    assert retried.json()["detail"]["code"] == "run_not_retryable"


def test_http_answer_request_id_replays_accepted_resume(tmp_path):
    repository = SQLiteRuntimeRepository(tmp_path / "answer-idempotency.sqlite3")
    calls = []
    pending = {
        "tool_call_id": "ask-budget",
        "step": 1,
        "questions": [{
            "id": "budget", "prompt": "预算是多少？", "options": [],
            "allow_custom": True, "required": True,
        }],
    }

    def fake_runner(**kwargs):
        calls.append(kwargs)
        if kwargs.get("resume_checkpoint") is None:
            return {
                "status": "waiting_user",
                "checkpoint": {"checkpoint_version": 2, "pending_input": pending},
                "state": {"runtime_status": "waiting_user", "pending_input": pending},
            }
        return {"status": "done", "summary": "根据预算继续分析。"}

    client = TestClient(create_app(runtime_repository=repository, runner=fake_runner))
    created = client.post("/v1/runs", json={"input": "帮我选车", "filters": {}})
    assert created.status_code == 202
    run_id = created.json()["run_id"]
    for _ in range(100):
        snapshot = client.get(f"/v1/runs/{run_id}").json()
        if snapshot["status"] == "waiting_user":
            break
        time.sleep(0.01)
    assert snapshot["status"] == "waiting_user"

    answer = {
        "client_request_id": "answer-budget-1",
        "tool_call_id": "ask-budget",
        "resume_revision": 0,
        "answers": [{"question_id": "budget", "values": ["15"]}],
    }
    first = client.post(f"/v1/runs/{run_id}/input", json=answer)
    replay = client.post(f"/v1/runs/{run_id}/input", json=answer)
    assert first.status_code == replay.status_code == 202
    assert replay.json()["accepted_tool_call_id"] == "ask-budget"
    assert replay.json()["resume_revision"] == first.json()["resume_revision"]
    conflict = client.post(
        f"/v1/runs/{run_id}/input",
        json={**answer, "answers": [{"question_id": "budget", "values": ["20"]}]},
    )
    assert conflict.status_code == 409
    for _ in range(100):
        if repository.get_run(run_id)["status"] == "done":
            break
        time.sleep(0.01)
    assert repository.get_run(run_id)["status"] == "done"
    assert len(calls) == 2


def test_waiting_run_accepts_steering_and_schedules_closed_batch_once(tmp_path, monkeypatch):
    import backend.service as service_module
    monkeypatch.setattr(service_module, "MIDRUN_INPUT_ENABLED", True)
    repository = SQLiteRuntimeRepository(tmp_path / "steering-http.sqlite3")
    first_state = AgentState.create("choose", "system")
    first_state.task_context = initial_task_context("choose")
    first_state.evidence_records = [{"evidence_id": "old", "constraint_revision": 0}]
    first_state.flags.update({"answer_finalized": True, "validated_answer": "old answer",
                              "answer_validation": {"valid": True}})
    first_state.messages.append({"role": "assistant", "tool_calls": [{
        "id": "ask-1", "type": "function", "function": {"name": "ask_user", "arguments": "{}"},
    }]})
    first_state.pending_input = {"tool_call_id": "ask-1", "questions": [{"id": "budget"}], "step": 1}
    first_state.pending_tool_batch = {"batch_id": "batch-1", "assistant_message_index": len(first_state.messages) - 1, "calls": [{
        "id": "ask-1", "type": "function", "function": {"name": "ask_user", "arguments": "{}"},
    }], "next_index": 0, "completed_call_ids": [], "step": 1}

    def fake_runner(**kwargs):
        if kwargs.get("resume_checkpoint") is None:
            return {"status": "waiting_user", "checkpoint": first_state.to_checkpoint(),
                    "state": first_state.to_dict()}
        state = AgentState.from_checkpoint(kwargs["resume_checkpoint"])
        steering = kwargs["steering_message"]
        append_user_steering(state, steering["content"], steering["client_message_id"], steering["revision"])
        assert state.task_context["revision"] == 1
        assert state.task_context["constraints"]["budget_max_wan"]["value"] == 15.0
        assert state.evidence_records[0]["constraint_revision"] != state.task_context["revision"]
        assert _current_evidence(state) == []
        assert not state.flags.get("answer_finalized")
        assert not state.flags.get("validated_answer")
        assert kwargs["message_ack"]([steering], state.to_checkpoint())
        validate_message_protocol(state.messages)
        assert state.messages[-1] == {"role": "user", "content": steering["content"]}
        return {"status": "done", "summary": "corrected", "state": state.to_dict()}

    client = TestClient(create_app(runtime_repository=repository, runner=fake_runner))
    created = client.post("/v1/runs", json={"run_id": "run-steer-http", "input": "choose"})
    assert created.status_code == 202
    run_id = created.json()["run_id"]
    for _ in range(100):
        snapshot = client.get(f"/v1/runs/{run_id}").json()
        if snapshot["status"] == "waiting_user":
            break
        time.sleep(0.01)
    assert snapshot["status"] == "waiting_user"
    scheduler = client.app.state.scheduler
    real_submit = scheduler.submit
    def submit_after_queue_full(value):
        return SubmitResult(True, "queued", "deferred_capacity")
    monkeypatch.setattr(scheduler, "submit", submit_after_queue_full)
    payload = {"client_message_id": "fix-1", "content": "budget is 15", "expected_revision": 0}
    accepted = client.post(f"/v1/runs/{run_id}/messages", json=payload)
    assert accepted.status_code == 202
    assert accepted.json()["revision"] == accepted.json()["input_revision"] == 1
    duplicate = client.post(f"/v1/runs/{run_id}/messages", json=payload)
    assert duplicate.status_code == 202
    assert duplicate.json()["revision"] == 1
    monkeypatch.setattr(scheduler, "submit", real_submit)
    for _ in range(100):
        snapshot = client.get(f"/v1/runs/{run_id}").json()
        if snapshot["status"] == "done":
            break
        time.sleep(0.01)
    assert snapshot["status"] == "done"
    events = repository.read_run_events(run_id)
    assert [event["event_type"] for event in events].count("message_received") == 1
    assert [event["event_type"] for event in events].count("message_applied") == 1
    assert all("budget is 15" not in json.dumps(event) for event in events)


def test_service_starts_run_and_replays_sse_from_last_event(tmp_path):
    run_store = RunStore(tmp_path / "runs")
    session_store = SessionStore(tmp_path / "sessions")

    def fake_runner(
        *, user_text, session_id, context_id, run_id, session_store, event_sink, confirmed_filters,
        owner_id, context_budget, token_estimator
    ):
        event_sink({"event_type": "model_round", "step": 1, "output": {"ok": True}})
        return {
            "summary": "完成",
            "trace_id": "trace-1",
            "state": {"flags": {"steps": 1}},
            "report": {"id": "run-report", "intent": "recommend"},
            "confirmed_filters": confirmed_filters,
            "context_id": context_id,
        }

    client = TestClient(create_app(run_store=run_store, session_store=session_store, runner=fake_runner))
    response = client.post(
        "/v1/runs",
        json={
            "session_id": "session-1",
            "context_id": "context-1",
            "input": "查询宝马",
            "filters": {"budget_max_wan": 15, "city": "北京"},
        },
    )

    assert response.status_code == 202
    run_id = response.json()["run_id"]
    for _ in range(50):
        if client.get(f"/v1/runs/{run_id}").json()["status"] == "done":
            break
        time.sleep(0.01)
    assert client.get(f"/v1/runs/{run_id}").json()["status"] == "done"

    started_event = run_store.read_after(run_id)[0]
    assert started_event["event_type"] == "started"
    assert started_event["queue_wait_ms"] >= 0
    assert started_event["attempt"] == 1
    events = client.get(f"/v1/runs/{run_id}/events").text
    assert "event: model_round" in events
    assert "event: done" in events
    assert '"queue_wait_ms"' in events
    first_seq = run_store.read_after(run_id)[0]["seq"]
    replay = client.get(f"/v1/runs/{run_id}/events", headers={"Last-Event-ID": str(first_seq)})
    assert f"id: {first_seq}" not in replay.text
    snapshot = json.loads(client.get(f"/v1/runs/{run_id}").text)
    assert snapshot["result"]["summary"] == "完成"
    assert snapshot["result"]["confirmed_filters"] == {"budget_max_wan": 15, "city": "北京"}
    assert snapshot["result"]["context_id"] == "context-1"
    assert snapshot["result"]["report"]["intent"] == "recommend"


def test_service_sse_done_event_is_compact_and_replayable(tmp_path):
    runs = RunStore(tmp_path / "runs")
    sessions = SessionStore(tmp_path / "sessions")

    def fake_runner(*, context_id, event_sink, **_kwargs):
        event_sink({"event_type": "model_round", "reasoning_content": "不要暴露"})
        return {
            "summary": "完成",
            "final_message": {"role": "assistant", "content": "答案"},
            "report": {"id": "report-sse", "summaryText": "结论"},
            "context_id": context_id,
            "state": {"large": "x" * 500_000},
            "run_logs": [{"tool": "recommend", "result": "x" * 50_000}],
            "tool_schemas": [{"name": "recommend"}],
        }

    client = TestClient(create_app(run_store=runs, session_store=sessions, runner=fake_runner))
    created = client.post("/v1/runs", json={"run_id": "run-sse-compact", "input": "测试"})
    assert created.status_code == 202
    for _ in range(50):
        if client.get("/v1/runs/run-sse-compact").json()["status"] == "done":
            break
        time.sleep(0.01)

    response = client.get("/v1/runs/run-sse-compact/events")
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"
    assert response.headers["x-run-status"] == "replayable"
    assert "event: done" in response.text
    done_line = next(line.removeprefix("data: ") for line in response.text.splitlines() if line.startswith("data: ") and '"event_type": "done"' in line)
    done_event = json.loads(done_line)
    assert done_event["result"]["report"]["id"] == "report-sse"
    assert "run_logs" not in done_event["result"]
    assert "tool_schemas" not in done_event["result"]
    assert "reasoning_content" not in json.dumps(done_event, ensure_ascii=False)
    assert len(response.content) < 64 * 1024


def test_public_run_snapshot_and_sse_project_internal_fields(tmp_path):
    runs = RunStore(tmp_path / "runs")
    sessions = SessionStore(tmp_path / "sessions")
    secret_marker = "private-agent-payload-42f0a6"
    sessions.append_messages(
        "session-projection",
        [
            {"role": "user", "content": "检查会话消息"},
            {
                "role": "assistant",
                "content": "可见答复",
                "reasoning_content": secret_marker,
                "tool_calls": [{"id": "call-secret", "function": {"name": "recommend", "arguments": secret_marker}}],
            },
            {
                "role": "tool",
                "tool_call_id": "call-secret",
                "name": "recommend",
                "content": json.dumps({"seller_private": secret_marker}),
            },
        ],
    )

    def fake_runner(*, context_id, event_sink, **_kwargs):
        event_sink(
            {
                "event_type": "model_round",
                "step": 1,
                "input": {"messages": [{"content": secret_marker}]},
                "output": {"reasoning_content": secret_marker},
                "reasoning_content": secret_marker,
                "status": "ok",
                "duration_ms": 7,
            }
        )
        event_sink(
            {
                "event_type": "tool_started",
                "step": 1,
                "input": {"tool_call_id": "call-1", "tool_name": "recommend", "arguments": secret_marker},
                "output": {"private": secret_marker},
                "status": "running",
            }
        )
        event_sink(
            {
                "event_type": "tool_completed",
                "step": 1,
                "input": {"tool_call_id": "call-1", "tool_name": "recommend", "arguments": secret_marker},
                "output": {"ok": True, "note": "工具已返回", "raw": secret_marker},
                "status": "done",
            }
        )
        return {
            "summary": "完成",
            "summary_source": "agent",
            "final_message": {"role": "assistant", "content": "答案", "reasoning_content": secret_marker},
            "report": {"id": "report-public", "summaryText": "结论", "debug_dump": secret_marker},
            "context_id": context_id,
            "state": {"messages": [{"reasoning_content": secret_marker}]},
            "checkpoint": {"messages": [{"reasoning_content": secret_marker}]},
            "trace": {"events": [{"reasoning_content": secret_marker}]},
            "run_logs": [{"raw": secret_marker}],
            "tool_schemas": [{"private": secret_marker}],
            "tool_results": [{"output": secret_marker}],
            "facts": [secret_marker],
        }

    client = TestClient(create_app(run_store=runs, session_store=sessions, runner=fake_runner))
    created = client.post(
        "/v1/runs",
        json={"run_id": "run-public-projection", "session_id": "session-projection", "input": "投影检查"},
    )
    assert created.status_code == 202
    for _ in range(50):
        if client.get("/v1/runs/run-public-projection").json()["status"] == "done":
            break

    snapshot = client.get("/v1/runs/run-public-projection").json()
    events = client.get("/v1/runs/run-public-projection/events").text
    messages = client.get("/v1/sessions/session-projection/messages").text
    public_payload = json.dumps({"snapshot": snapshot, "events": events, "messages": messages}, ensure_ascii=False)

    assert secret_marker not in public_payload
    assert "checkpoint" not in snapshot
    assert "state" not in snapshot.get("result", {})
    assert "trace" not in snapshot.get("result", {})
    assert "tool_results" not in snapshot.get("result", {})
    assert "reasoning_content" not in public_payload
    assert "arguments" not in events
    assert snapshot["result"]["report"]["id"] == "report-public"


def test_public_run_event_projection_uses_event_specific_allowlists():
    from backend.api_projection import public_run_event

    cases = [
        (
            {"seq": 1, "event_type": "plan_update", "input": {"private": "x"}, "output": {"steps": [{"id": "a", "title": "查价格", "tool_names": ["price_compare"], "status": "running", "private": "x"}], "private": "x"}, "reasoning_content": "x"},
            {"seq", "event_type", "output"},
        ),
        (
            {"seq": 2, "event_type": "input_required", "input": {"tool_call_id": "call-2", "plan_step_id": "a", "arguments": "x"}, "output": {"questions": [{"id": "q1", "prompt": "预算？", "options": [{"id": "a", "label": "15 万", "value": "15", "private": "x"}], "allow_custom": True, "selection_mode": "single", "private": "x"}], "private": "x"}, "checkpoint": "x"},
            {"seq", "event_type", "input", "output"},
        ),
        (
            {"seq": 3, "event_type": "tool_started", "step": 2, "input": {"tool_call_id": "call-3", "tool_name": "recommend", "plan_step_id": "b", "arguments": "x"}, "output": {"private": "x"}, "status": "running", "duration_ms": 0},
            {"seq", "event_type", "step", "input", "status", "duration_ms"},
        ),
        (
            {"seq": 4, "event_type": "tool_completed", "step": 2, "input": {"tool_call_id": "call-3", "tool_name": "recommend", "plan_step_id": "b", "arguments": "x"}, "output": {"ok": True, "note": "返回三款", "raw": "x"}, "status": "done", "duration_ms": 8},
            {"seq", "event_type", "step", "input", "output", "status", "duration_ms"},
        ),
        (
            {"seq": 5, "event_type": "model_round", "step": 3, "input": {"messages": ["x"]}, "output": {"reasoning_content": "x"}, "status": "ok", "duration_ms": 11},
            {"seq", "event_type", "step", "status", "duration_ms"},
        ),
        (
            {"seq": 6, "event_type": "final", "output": {"content": "已完成", "private": "x"}, "reasoning_content": "x"},
            {"seq", "event_type", "output"},
        ),
        (
            {"seq": 7, "event_type": "done", "result": {"summary": "完成", "state": {"private": "x"}, "report": {"id": "r", "debug_dump": "x"}}},
            {"seq", "event_type", "result"},
        ),
        (
            {"seq": 8, "event_type": "error", "error": "运行失败", "trace": "x", "private": "x"},
            {"seq", "event_type", "error"},
        ),
    ]

    for event, expected_keys in cases:
        projected = public_run_event(event)
        assert set(projected) == expected_keys
        encoded = json.dumps(projected, ensure_ascii=False)
        for private_field in ("private", "arguments", "reasoning_content", "checkpoint", "state", "trace"):
            assert private_field not in encoded


def test_health_endpoint_is_available(tmp_path):
    client = TestClient(create_app(run_store=RunStore(tmp_path / "runs"), session_store=SessionStore(tmp_path / "sessions")))
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_create_run_rejects_oversized_input_before_scheduling(tmp_path):
    runs = RunStore(tmp_path / "runs")
    client = TestClient(
        create_app(
            run_store=runs,
            session_store=SessionStore(tmp_path / "sessions"),
            context_budget=ContextBudget(10_000, 1_000, 1_000, 2, 600),
        )
    )

    response = client.post("/v1/runs", json={"input": "问题" * 4_000})

    assert response.status_code == 413
    assert response.json()["detail"]["message"] == "context_input_too_large"
    assert runs.list() == []


def test_create_run_rejects_configured_character_limit_before_scheduling(tmp_path, monkeypatch):
    import backend.service as service_module

    runs = RunStore(tmp_path / "runs")
    monkeypatch.setattr(service_module, "CONTEXT_MAX_INPUT_CHARACTERS", 7)
    client = TestClient(create_app(run_store=runs, session_store=SessionStore(tmp_path / "sessions")))

    response = client.post("/v1/runs", json={"input": "问题内容超过限制"})

    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "input_too_long"
    assert response.json()["detail"]["max_characters"] == 7
    assert runs.list() == []


def test_service_can_run_end_to_end_on_shared_sqlite_repository(tmp_path):
    repository = SQLiteRuntimeRepository(tmp_path / "runtime.sqlite3")

    def fake_runner(**kwargs):
        kwargs["session_store"].append_messages(
            kwargs["context_id"],
            [{"role": "user", "content": kwargs["user_text"]}, {"role": "assistant", "content": "SQLite 完成"}],
            run_id=kwargs["run_id"],
            owner_id=kwargs["owner_id"],
        )
        kwargs["event_sink"]({"event_type": "final", "output": {"content": "SQLite 完成"}})
        return {"summary": "SQLite 完成", "report": {"id": kwargs["run_id"], "intent": "unknown"}}

    app = create_app(
        runtime_repository=repository,
        runner=fake_runner,
        owner_cookie_secret="sqlite-service-secret",
    )
    assert isinstance(app.state.run_store, SQLiteRunStore)
    assert isinstance(app.state.session_store, SQLiteSessionStore)
    client = TestClient(app)
    response = client.post(
        "/v1/runs",
        json={"run_id": "run-sqlite", "session_id": "session-sqlite", "input": "问题"},
    )
    assert response.status_code == 202
    for _ in range(100):
        if client.get("/v1/runs/run-sqlite").json()["status"] == "done":
            break
        time.sleep(0.005)

    assert client.get("/v1/runs/run-sqlite").json()["result"]["summary"] == "SQLite 完成"
    assert "event: done" in client.get("/v1/runs/run-sqlite/events").text
    assert [item["content"] for item in client.get("/v1/sessions/session-sqlite/messages").json()["messages"]] == [
        "问题",
        "SQLite 完成",
    ]


def test_health_runtime_readiness_and_owner_scoped_cancel(tmp_path):
    repository = SQLiteRuntimeRepository(tmp_path / "runtime-health.sqlite3")
    entered = threading.Event()
    release = threading.Event()

    def fake_runner(**_kwargs):
        entered.set()
        release.wait(1)
        return {"summary": "完成"}

    client = TestClient(create_app(runtime_repository=repository, runner=fake_runner, owner_cookie_secret="health-secret"))
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/health/ready").status_code == 200
    runtime_health = client.get("/health/runtime").json()
    assert runtime_health["storage"] == "sqlite"
    assert runtime_health["runtime_protocol_version"] == 3
    assert runtime_health["checkpoint_version"] == 4
    assert runtime_health["answer_schema_version"] == 2
    assert len(runtime_health["prompt_sha256"]) == 16
    created = client.post("/v1/runs", json={"run_id": "run-cancel-health", "input": "取消我"})
    assert created.status_code == 202
    assert entered.wait(1)
    cancelled = client.post("/v1/runs/run-cancel-health/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert client.post("/v1/runs/run-cancel-health/cancel").json()["status"] == "cancelled"
    release.set()


def test_cancel_rejects_other_owner(tmp_path):
    runs = RunStore(tmp_path / "runs")
    sessions = SessionStore(tmp_path / "sessions")
    client = TestClient(create_app(run_store=runs, session_store=sessions, runner=lambda **_: {"summary": "done"}, owner_cookie_secret="cancel-owner-secret"))
    created = client.post("/v1/runs", json={"run_id": "run-owner-cancel", "input": "问题"})
    assert created.status_code == 202
    other = TestClient(create_app(run_store=runs, session_store=sessions, runner=lambda **_: {"summary": "done"}, owner_cookie_secret="cancel-owner-secret"))
    other.cookies.set("uca_owner", "invalid")
    assert other.post("/v1/runs/run-owner-cancel/cancel").status_code == 404


def test_same_run_id_is_claimed_only_once(tmp_path):
    runs = RunStore(tmp_path / "runs")
    sessions = SessionStore(tmp_path / "sessions")
    calls = []

    def fake_runner(**kwargs):
        calls.append(kwargs["run_id"])
        return {"summary": "一次"}

    client = TestClient(create_app(run_store=runs, session_store=sessions, runner=fake_runner))
    first = client.post("/v1/runs", json={"run_id": "run-once", "input": "问题"})
    second = client.post("/v1/runs", json={"run_id": "run-once", "input": "问题"})

    for _ in range(50):
        if client.get("/v1/runs/run-once").json()["status"] == "done":
            break
    assert first.status_code == second.status_code == 202
    assert calls == ["run-once"]


def test_history_endpoint_returns_session_records_latest_first(tmp_path):
    runs = RunStore(tmp_path / "runs")
    secret = "history-owner-secret"
    client = TestClient(create_app(run_store=runs, session_store=SessionStore(tmp_path / "sessions"), owner_cookie_secret=secret))
    client.get("/v1/history", params={"session_id": "bootstrap"})
    owner_id = owner_id_from_cookie(client.cookies.get("uca_owner"), secret)
    assert owner_id
    first = runs.create("run-history-1", "session-history", "第一条", owner_id=owner_id)
    runs.finish(
        first["run_id"],
        result={
            "summary": "第一条结论",
            "report": {
                "id": "run-history-1",
                "intent": "recommend",
                "scenarioLabel": "智能购车",
                "summaryText": "第一条结论",
                "reliability": {"confidence": "high"},
            },
        },
    )
    time.sleep(0.01)
    second = runs.create("run-history-2", "session-history", "第二条", owner_id=owner_id)
    runs.finish(
        second["run_id"],
        result={
            "summary": "第二条结论",
            "report": {
                "id": "run-history-2",
                "intent": "price_compare",
                "scenarioLabel": "车型行情",
                "summaryText": "第二条结论",
                "reliability": {"confidence": "medium"},
            },
        },
    )
    other = runs.create("run-history-3", "session-other", "不应返回", owner_id=owner_id)
    runs.finish(other["run_id"], result={"summary": "其他"})

    response = client.get("/v1/history", params={"session_id": "session-history"})

    assert response.status_code == 200
    assert [item["id"] for item in response.json()] == ["run-history-2", "run-history-1"]
    assert response.json()[0]["reportId"] == "run-history-2"
    assert response.json()[0]["intent"] == "price_compare"


def test_history_endpoint_marks_run_without_report_as_unknown(tmp_path):
    runs = RunStore(tmp_path / "runs")
    secret = "history-owner-secret"
    client = TestClient(create_app(run_store=runs, session_store=SessionStore(tmp_path / "sessions"), owner_cookie_secret=secret))
    client.get("/v1/history", params={"session_id": "bootstrap"})
    owner_id = owner_id_from_cookie(client.cookies.get("uca_owner"), secret)
    assert owner_id
    run = runs.create("run-no-report", "session-no-report", "边界问题", owner_id=owner_id)
    runs.finish(run["run_id"], result={"summary": "当前能力有限"})

    response = client.get("/v1/history", params={"session_id": "session-no-report"})

    assert response.status_code == 200
    assert response.json()[0]["intent"] == "unknown"
    assert response.json()[0]["reportId"] == "run-no-report"


def test_local_frontend_origin_is_allowed_for_api_calls(tmp_path):
    client = TestClient(create_app(run_store=RunStore(tmp_path / "runs"), session_store=SessionStore(tmp_path / "sessions")))

    response = client.options(
        "/v1/runs",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,last-event-id",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert response.headers["access-control-allow-credentials"] == "true"


def test_run_snapshot_preserves_history_and_agent_context_ids(tmp_path):
    runs = RunStore(tmp_path / "runs")
    sessions = SessionStore(tmp_path / "sessions")
    seen = []

    def fake_runner(**kwargs):
        seen.append((kwargs["session_id"], kwargs["context_id"]))
        return {"summary": "完成", "context_id": kwargs["context_id"]}

    client = TestClient(create_app(run_store=runs, session_store=sessions, runner=fake_runner))
    response = client.post(
        "/v1/runs",
        json={"session_id": "history-1", "context_id": "context-1", "input": "独立问题", "filters": {}},
    )

    assert response.status_code == 202
    run_id = response.json()["run_id"]
    for _ in range(50):
        if client.get(f"/v1/runs/{run_id}").json()["status"] == "done":
            break
        time.sleep(0.01)
    snapshot = client.get(f"/v1/runs/{run_id}").json()
    assert seen == [("history-1", "context-1")]
    assert snapshot["session_id"] == "history-1"
    assert snapshot["context_id"] == "context-1"


def test_waiting_run_accepts_structured_answers_and_resumes_same_run(tmp_path):
    runs = RunStore(tmp_path / "runs")
    sessions = SessionStore(tmp_path / "sessions")
    calls = []

    def fake_runner(**kwargs):
        calls.append(kwargs)
        if kwargs.get("resume_checkpoint") is None:
            kwargs["event_sink"](
                {
                    "event_type": "input_required",
                    "pending_input": {
                        "questions": [
                            {
                                "id": "budget",
                                "prompt": "预算？",
                                "options": [{"id": "a", "label": "15 万", "value": "15"}],
                                "allow_custom": True,
                                "required": True,
                            }
                        ],
                        "tool_call_id": "call-ask",
                        "step": 1,
                    },
                }
            )
            return {
                "status": "waiting_user",
                "checkpoint": {
                    "trace_id": "trace-wait",
                    "user_text": "帮我选车",
                    "messages": [{"role": "system", "content": "系统"}, {"role": "user", "content": "帮我选车"}],
                    "pending_input": {
                        "questions": [
                            {
                                "id": "budget",
                                "prompt": "预算？",
                                "options": [{"id": "a", "label": "15 万", "value": "15"}],
                                "allow_custom": True,
                                "required": True,
                            }
                        ],
                        "tool_call_id": "call-ask",
                        "step": 1,
                    },
                },
                    "state": {
                        "runtime_status": "waiting_user",
                        "pending_input": {
                            "questions": [{
                                "id": "budget", "prompt": "预算？",
                                "options": [{"id": "a", "label": "15 万", "value": "15"}],
                                "allow_custom": True, "required": True,
                            }],
                            "tool_call_id": "call-ask", "step": 1,
                        },
                    },
            }
        assert kwargs["input_answers"] == [{"question_id": "budget", "values": ["15"]}]
        kwargs["event_sink"]({"event_type": "final", "content": "完成"})
        return {"status": "done", "summary": "完成"}

    client = TestClient(create_app(run_store=runs, session_store=sessions, runner=fake_runner))
    created = client.post(
        "/v1/runs",
        json={"session_id": "history-wait", "context_id": "context-wait", "input": "帮我选车", "filters": {}},
    )
    assert created.status_code == 202
    run_id = created.json()["run_id"]
    for _ in range(50):
        if client.get(f"/v1/runs/{run_id}").json()["status"] == "waiting_user":
            break
        time.sleep(0.01)
    waiting = client.get(f"/v1/runs/{run_id}").json()
    assert waiting["status"] == "waiting_user"
    assert "event: input_required" in client.get(f"/v1/runs/{run_id}/events").text
    other_owner = TestClient(client.app)
    assert other_owner.post(
        f"/v1/runs/{run_id}/input",
        json={"answers": [{"question_id": "budget", "value": "15"}]},
    ).status_code == 404

    invalid = client.post(f"/v1/runs/{run_id}/input", json={"answers": []})
    assert invalid.status_code == 400
    assert client.get(f"/v1/runs/{run_id}").json()["status"] == "waiting_user"

    resumed = client.post(
        f"/v1/runs/{run_id}/input",
        json={"answers": [{"question_id": "budget", "value": "15"}]},
    )
    assert resumed.status_code == 202
    for _ in range(50):
        if client.get(f"/v1/runs/{run_id}").json()["status"] == "done":
            break
        time.sleep(0.01)
    assert client.get(f"/v1/runs/{run_id}").json()["status"] == "done"
    assert len(calls) == 2


def test_submit_input_returns_resume_barrier_and_exposes_a_new_question(tmp_path):
    runs = RunStore(tmp_path / "runs")
    sessions = SessionStore(tmp_path / "sessions")
    calls = []

    def question(call_id: str) -> dict:
        return {
            "questions": [
                {
                    "id": call_id,
                    "prompt": f"问题 {call_id}",
                    "options": [{"id": "a", "label": "选项 A", "value": "A"}],
                    "allow_custom": False,
                    "required": True,
                }
            ],
            "tool_call_id": call_id,
            "step": 1,
        }

    def fake_runner(**kwargs):
        calls.append(kwargs)
        call_id = "call-a" if len(calls) == 1 else "call-b"
        pending = question(call_id)
        kwargs["event_sink"]({"event_type": "input_required", "input": {"tool_call_id": call_id}, "pending_input": pending})
        return {
            "status": "waiting_user",
            "checkpoint": {"pending_input": pending, "messages": [{"role": "system", "content": "系统"}]},
            "state": {"pending_input": pending, "plan": [], "run_logs": []},
        }

    client = TestClient(create_app(run_store=runs, session_store=sessions, runner=fake_runner))
    created = client.post("/v1/runs", json={"run_id": "run-barrier", "input": "连续提问"})
    run_id = created.json()["run_id"]
    for _ in range(100):
        if client.get(f"/v1/runs/{run_id}").json().get("status") == "waiting_user":
            break
        time.sleep(0.005)

    first = client.post(
        f"/v1/runs/{run_id}/input",
        json={"tool_call_id": "call-a", "resume_revision": 0, "answers": [{"question_id": "call-a", "value": "A"}]},
    )
    assert first.status_code == 202
    assert first.json()["accepted_tool_call_id"] == "call-a"
    assert first.json()["resume_revision"] == 1

    for _ in range(100):
        snapshot = client.get(f"/v1/runs/{run_id}").json()
        if snapshot.get("status") == "waiting_user" and snapshot.get("pending_input", {}).get("tool_call_id") == "call-b":
            break
        time.sleep(0.005)
    assert snapshot["status"] == "waiting_user"
    assert snapshot["pending_input"]["tool_call_id"] == "call-b"
    assert any(
        event.get("event_type") == "input_required"
        and event.get("resume_revision") == 1
        for event in runs.read_after(run_id)
    )

    second = client.post(
        f"/v1/runs/{run_id}/input",
        json={"tool_call_id": "call-b", "resume_revision": 1, "answers": [{"question_id": "call-b", "value": "A"}]},
    )
    assert second.status_code == 202
    assert second.json()["accepted_tool_call_id"] == "call-b"
    assert second.json()["resume_revision"] == 2


def test_input_required_is_not_exposed_before_waiting_state_and_duplicate_input_is_rejected(tmp_path):
    runs = RunStore(tmp_path / "runs")
    sessions = SessionStore(tmp_path / "sessions")
    calls = []
    pending = {
        "questions": [
            {
                "id": "budget",
                "prompt": "预算？",
                "options": [{"id": "a", "label": "15 万", "value": "15"}],
                "allow_custom": True,
                "required": True,
            }
        ],
        "tool_call_id": "call-ask-race",
        "step": 1,
    }

    def fake_runner(**kwargs):
        calls.append(kwargs)
        if kwargs.get("resume_checkpoint") is None:
            kwargs["event_sink"]({"event_type": "input_required", "pending_input": pending})
            # Widen the exact window in which the old worker exposed the event
            # while the RunStore status was still "running".
            time.sleep(0.05)
            return {
                "status": "waiting_user",
                "checkpoint": {
                    "trace_id": "trace-race",
                    "user_text": "帮我选车",
                    "messages": [{"role": "system", "content": "系统"}, {"role": "user", "content": "帮我选车"}],
                    "pending_input": pending,
                },
                "state": {"pending_input": pending, "runtime_status": "waiting_user"},
            }
        return {"status": "done", "summary": "完成"}

    client = TestClient(create_app(run_store=runs, session_store=sessions, runner=fake_runner))
    created = client.post(
        "/v1/runs",
        json={"session_id": "history-race", "context_id": "context-race", "input": "帮我选车", "filters": {}},
    )
    run_id = created.json()["run_id"]

    waiting = None
    for _ in range(100):
        events = runs.read_after(run_id)
        if any(event.get("event_type") == "input_required" for event in events):
            snapshot = client.get(f"/v1/runs/{run_id}").json()
            if snapshot["status"] == "waiting_user":
                waiting = snapshot
                break
        time.sleep(0.005)

    assert waiting is not None
    assert waiting["status"] == "waiting_user"
    answer = {"answers": [{"question_id": "budget", "value": "15"}]}
    first = client.post(f"/v1/runs/{run_id}/input", json=answer)
    second = client.post(f"/v1/runs/{run_id}/input", json=answer)
    assert first.status_code == 202
    assert second.status_code == 409

    for _ in range(100):
        if client.get(f"/v1/runs/{run_id}").json()["status"] == "done":
            break
        time.sleep(0.005)
    assert client.get(f"/v1/runs/{run_id}").json()["status"] == "done"
    assert len(calls) == 2


def test_sqlite_waiting_transition_atomically_persists_input_required_event(tmp_path):
    repository = SQLiteRuntimeRepository(tmp_path / "waiting-atomic.sqlite3")
    pending = {
        "tool_call_id": "call-atomic-wait",
        "step": 1,
        "questions": [{
            "id": "budget",
            "prompt": "预算是多少？",
            "options": [{"id": "a", "label": "15 万", "value": "15"}],
            "allow_custom": True,
            "required": True,
        }],
    }

    def fake_runner(**kwargs):
        kwargs["event_sink"]({"event_type": "input_required", "pending_input": pending})
        return {
            "status": "waiting_user",
            "checkpoint": {"pending_input": pending, "messages": [{"role": "system", "content": "system"}]},
            "state": {"pending_input": pending, "plan": [], "run_logs": [], "flags": {}},
        }

    client = TestClient(create_app(runtime_repository=repository, runner=fake_runner))
    response = client.post("/v1/runs", json={"run_id": "atomic-wait", "input": "找一辆车"})
    assert response.status_code == 202
    run_id = response.json()["run_id"]

    snapshot = None
    events = []
    for _ in range(200):
        snapshot = client.get(f"/v1/runs/{run_id}").json()
        events = repository.read_run_events(run_id)
        if snapshot.get("status") == "waiting_user":
            break
        time.sleep(0.005)

    assert snapshot is not None
    assert snapshot["status"] == "waiting_user"
    assert snapshot["pending_input"]["tool_call_id"] == "call-atomic-wait"
    waiting_events = [event for event in events if event.get("event_type") == "input_required"]
    assert len(waiting_events) == 1
    assert waiting_events[0]["pending_input"]["tool_call_id"] == "call-atomic-wait"
    assert waiting_events[0]["resume_revision"] == snapshot["resume_revision"]
    assert "event: input_required" in client.get(f"/v1/runs/{run_id}/events").text


def test_sqlite_answers_use_public_question_revision_across_consecutive_waits(tmp_path):
    repository = SQLiteRuntimeRepository(tmp_path / "question-barrier.sqlite3")
    calls = []

    def fake_runner(**kwargs):
        calls.append(kwargs)
        if len(calls) > 2:
            return {"status": "done", "summary": "完成", "state": {"plan": [], "run_logs": []}}
        pending = {"tool_call_id": f"ask-{len(calls)}", "resume_revision": len(calls), "step": 1,
                   "questions": [{"id": "choice", "prompt": "请选择", "options": [{"id": "a", "label": "A", "value": "A"}],
                                  "allow_custom": False, "required": True}]}
        return {"status": "waiting_user", "checkpoint": {"pending_input": pending, "messages": []},
                "state": {"pending_input": pending, "plan": [], "run_logs": []}}

    with TestClient(create_app(runtime_repository=repository, runner=fake_runner)) as client:
        run_id = client.post("/v1/runs", json={"input": "连续提问"}).json()["run_id"]
        for index in (1, 2):
            for _ in range(200):
                snapshot = client.get(f"/v1/runs/{run_id}").json()
                if snapshot["status"] == "waiting_user" and snapshot["pending_input"]["tool_call_id"] == f"ask-{index}":
                    break
                time.sleep(0.005)
            pending = snapshot["pending_input"]
            assert pending["resume_revision"] == snapshot["resume_revision"] == index - 1
            if index == 2:
                stale = client.post(f"/v1/runs/{run_id}/input", json={"tool_call_id": "ask-1", "resume_revision": 0,
                                    "answers": [{"question_id": "choice", "value": "A"}]})
                assert stale.status_code == 409
            body = {"tool_call_id": pending["tool_call_id"], "resume_revision": pending["resume_revision"],
                    "client_request_id": f"answer-{index}", "answers": [{"question_id": "choice", "value": "A"}]}
            accepted = client.post(f"/v1/runs/{run_id}/input", json=body)
            assert accepted.status_code == 202
            replay = client.post(f"/v1/runs/{run_id}/input", json=body)
            assert replay.status_code == 202
            assert replay.json()["resume_revision"] == accepted.json()["resume_revision"] == index
        for _ in range(200):
            if client.get(f"/v1/runs/{run_id}").json()["status"] == "done":
                break
            time.sleep(0.005)
        assert len(calls) == 3


def test_sqlite_http_completion_commits_session_delta_with_terminal_result(tmp_path):
    repository = SQLiteRuntimeRepository(tmp_path / "complete-session-http.sqlite3")
    calls = []

    def fake_runner(**kwargs):
        calls.append(kwargs["run_id"])
        return {
            "status": "done",
            "summary": "已完成",
            "state": {"plan": [], "run_logs": []},
            "_session_commit": {
                "messages": [
                    {"role": "user", "content": kwargs["user_text"]},
                    {"role": "assistant", "content": "已完成"},
                ],
                "run_id": f"{kwargs['run_id']}-attempt-{kwargs['execution_attempt']}",
            },
        }

    client = TestClient(create_app(runtime_repository=repository, runner=fake_runner))
    created = client.post(
        "/v1/runs",
        json={"run_id": "run-session-commit-http", "session_id": "history-http", "context_id": "context-http", "input": "检查提交"},
    )
    assert created.status_code == 202
    run_id = created.json()["run_id"]
    duplicate = client.post(
        "/v1/runs",
        json={"run_id": run_id, "session_id": "history-http", "context_id": "context-http", "input": "检查提交"},
    )
    conflict = client.post(
        "/v1/runs",
        json={"run_id": run_id, "session_id": "history-http", "context_id": "context-http", "input": "不同请求"},
    )
    assert duplicate.status_code == 202
    assert duplicate.json()["run_id"] == run_id
    assert conflict.status_code == 409

    for _ in range(200):
        snapshot = client.get(f"/v1/runs/{run_id}").json()
        if snapshot["status"] in {"done", "error"}:
            break
        time.sleep(0.005)

    assert snapshot["status"] == "done"
    assert "_session_commit" not in snapshot["result"]
    messages = repository.load_session_owned("context-http", repository.get_run(run_id)["owner_id"]).messages
    assert [message["content"] for message in messages] == ["检查提交", "已完成"]
    assert [event["event_type"] for event in repository.read_run_events(run_id)][-1] == "done"
    assert calls == [run_id]

def test_json_compatibility_advertises_no_midrun_or_recovery_capabilities(tmp_path, monkeypatch):
    import backend.service as service_module
    monkeypatch.setattr(service_module, "MIDRUN_INPUT_ENABLED", True)
    monkeypatch.setattr(service_module, "RUN_RECOVERY_ENABLED", True)
    runs = RunStore(tmp_path / "json-runs")
    sessions = SessionStore(tmp_path / "json-sessions")
    client = TestClient(create_app(run_store=runs, session_store=sessions,
                                   runner=lambda **kwargs: {"status": "done", "summary": "done"}))
    created = client.post("/v1/runs", json={"run_id": "json-capabilities", "input": "question"})
    assert created.status_code == 202
    assert created.json()["capabilities"] == {"midrun_input": False, "recover": False}
    assert client.post("/v1/runs/json-capabilities/messages", json={
        "client_message_id": "m1", "content": "correction", "expected_revision": 0,
    }).status_code == 501
    assert client.post("/v1/runs/json-capabilities/recover", json={}).status_code == 501


def test_real_sqlite_busy_is_safe_retryable_and_preserves_first_owner(tmp_path):
    repository = SQLiteRuntimeRepository(tmp_path / "busy-http.sqlite3", busy_timeout_ms=25)
    app = create_app(runtime_repository=repository, runner=lambda **_: {"status": "done", "summary": "offline"})
    blocker = repository._connect()
    blocker.execute("BEGIN IMMEDIATE")
    try:
        client = TestClient(app)
        failed = client.post("/v1/runs", json={"input": "offline", "client_request_id": "busy-command"})
        assert failed.status_code == 503
        detail = failed.json()["detail"]
        assert detail["code"] == "storage_busy"
        assert detail["retryable"] is True
        assert detail["stage"] == "persistence"
        assert detail["request_id"] == failed.headers["x-request-id"]
        first_cookie = client.cookies.get("uca_owner")
        assert first_cookie and "set-cookie" in failed.headers
        assert repository.list_runs() == []
    finally:
        blocker.rollback()
        blocker.close()
    accepted = client.post("/v1/runs", json={"input": "offline", "client_request_id": "busy-command"})
    assert accepted.status_code == 202
    assert client.cookies.get("uca_owner") == first_cookie
    assert client.get(f"/v1/runs/{accepted.json()['run_id']}").status_code == 200
    app.state.scheduler.shutdown()


def test_committed_submission_survives_scheduler_notification_and_reread_failure(tmp_path, monkeypatch, caplog):
    repository = SQLiteRuntimeRepository(tmp_path / "committed-http.sqlite3")
    calls = []
    def offline_runner(**kwargs):
        calls.append(kwargs["run_id"])
        return {"status": "done", "summary": "offline"}
    app = create_app(runtime_repository=repository, runner=offline_runner, owner_cookie_secret="accepted-owner")
    def unavailable(*_args, **_kwargs):
        raise RuntimeError("private failure sk-secret-must-not-appear")
    monkeypatch.setattr(app.state.scheduler, "submit", unavailable)
    monkeypatch.setattr(app.state.run_event_notifier, "notify", unavailable)
    monkeypatch.setattr(app.state.run_store, "load", unavailable)
    client = TestClient(app)
    payload = {"input": "offline", "client_request_id": "durable-command"}
    accepted = client.post("/v1/runs", json=payload)
    repeated = client.post("/v1/runs", json=payload)
    assert accepted.status_code == repeated.status_code == 202
    assert accepted.json()["run_id"] == repeated.json()["run_id"]
    assert accepted.json()["status"] == "queued"
    assert "set-cookie" in accepted.headers
    assert "private failure" not in accepted.text
    assert len(repository.list_runs()) == 1
    owner = owner_id_from_cookie(client.cookies.get("uca_owner"), "accepted-owner")
    assert repository.get_run_owned(accepted.json()["run_id"], owner)["status"] == "queued"
    assert "durable run wake failed" in caplog.text
    assert app.state.scheduler.stats().healthy is False
    monkeypatch.undo()
    app.state.scheduler.start()
    for _ in range(100):
        if repository.get_run(accepted.json()["run_id"])["status"] == "done":
            break
        time.sleep(0.01)
    assert repository.get_run(accepted.json()["run_id"])["status"] == "done"
    assert calls == [accepted.json()["run_id"]]
    app.state.scheduler.shutdown()


def test_preflight_failure_has_safe_diagnostics_cookie_and_cors(tmp_path, monkeypatch):
    import backend.service as module
    repository = SQLiteRuntimeRepository(tmp_path / "preflight-http.sqlite3")
    app = create_app(runtime_repository=repository, owner_cookie_secret="test-stable-owner")
    def schema_failure():
        raise RuntimeError("secret schema details sk-hidden123456")
    monkeypatch.setattr(module, "stable_tool_schemas", schema_failure)
    client = TestClient(app)
    response = client.post("/v1/runs", json={"input": "offline"}, headers={"Origin": "http://127.0.0.1:5173"})
    assert response.status_code == 500
    assert response.json()["detail"]["code"] == "submission_preflight_failed"
    assert response.json()["detail"]["acceptance"] == "not_accepted"
    assert "sk-hidden" not in response.text
    assert "set-cookie" in response.headers
    assert response.headers["access-control-allow-origin"] == "http://127.0.0.1:5173"
    assert repository.list_runs() == []
    app.state.scheduler.shutdown()


def test_write_lock_does_not_block_http_event_loop(tmp_path):
    import asyncio
    import httpx
    repository = SQLiteRuntimeRepository(tmp_path / "responsive-http.sqlite3", busy_timeout_ms=300)
    app = create_app(runtime_repository=repository)
    blocker = repository._connect()
    blocker.execute("BEGIN IMMEDIATE")
    async def probe():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            post = asyncio.create_task(client.post("/v1/runs", json={"input": "offline"}))
            await asyncio.sleep(0.04)
            started = time.monotonic()
            health = await client.get("/health")
            assert health.status_code == 200
            assert time.monotonic() - started < 0.15
            assert not post.done()
            assert (await post).status_code == 503
    try:
        asyncio.run(probe())
    finally:
        blocker.rollback()
        blocker.close()
        app.state.scheduler.shutdown()


def test_history_exposes_authoritative_failure_retryability(tmp_path):
    repository = SQLiteRuntimeRepository(tmp_path / "history-failure-http.sqlite3")
    client = TestClient(create_app(runtime_repository=repository, owner_cookie_secret="history-owner"))
    client.get("/v1/capabilities")
    owner = owner_id_from_cookie(client.cookies.get("uca_owner"), "history-owner")
    repository.create_run("history-failure", "history-session", "offline", owner_id=owner)
    repository.finish_run("history-failure", error="failed", failure_code="storage_failure", retryable=True)
    response = client.get("/v1/history?session_id=history-session")
    assert response.status_code == 200
    assert response.json()[0]["retryable"] is True
    assert response.json()[0]["failureCode"] == "storage_failure"
    client.app.state.scheduler.shutdown()


def test_sync_submission_notifies_async_subscriber_across_thread_boundary(tmp_path, monkeypatch):
    import asyncio
    import httpx
    import backend.service as module
    monkeypatch.setattr(module, "stable_tool_schemas", lambda: [])
    repository = SQLiteRuntimeRepository(tmp_path / "thread-notify-http.sqlite3")
    app = create_app(runtime_repository=repository, runner=lambda **_: {"status": "done", "summary": "offline"})
    original_notify = app.state.run_event_notifier.notify
    notification_threads = []
    def notify(run_id):
        notification_threads.append(threading.get_ident())
        original_notify(run_id)
    monkeypatch.setattr(app.state.run_event_notifier, "notify", notify)
    async def probe():
        loop_thread = threading.get_ident()
        event = app.state.run_event_notifier.subscribe("thread-notify")
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                response = await client.post("/v1/runs", json={"input": "offline", "run_id": "thread-notify"})
                assert response.status_code == 202
                await asyncio.wait_for(event.wait(), timeout=1)
                assert notification_threads
                assert all(thread_id != loop_thread for thread_id in notification_threads)
        finally:
            app.state.run_event_notifier.unsubscribe("thread-notify", event)
            await asyncio.to_thread(app.state.scheduler.shutdown)
    asyncio.run(probe())


def test_sse_status_cursor_fallback_catches_events_when_notification_is_lost(tmp_path, monkeypatch):
    import backend.service as module
    monkeypatch.setenv("RUNTIME_SSE_POLL_INTERVAL_SECONDS", "0.05")
    monkeypatch.setattr(module, "stable_tool_schemas", lambda: [])
    repository = SQLiteRuntimeRepository(tmp_path / "sse-poll-fallback.sqlite3")
    app = create_app(runtime_repository=repository,
                     runner=lambda **_: (time.sleep(0.12) or {"status": "done", "summary": "offline"}))
    monkeypatch.setattr(app.state.run_event_notifier, "notify", lambda *_: None)
    cursor_reads = 0
    original_cursor = app.state.run_store.status_cursor
    def count_cursor(run_id):
        nonlocal cursor_reads
        cursor_reads += 1
        return original_cursor(run_id)
    monkeypatch.setattr(app.state.run_store, "status_cursor", count_cursor)
    client = TestClient(app)
    created = client.post("/v1/runs", json={"input": "offline"})
    assert created.status_code == 202
    response = client.get(f"/v1/runs/{created.json()['run_id']}/events")
    assert "event: done" in response.text
    assert cursor_reads >= 2
    app.state.scheduler.shutdown()


def test_changed_dataset_preflight_returns_stable_safe_retry_contract(tmp_path, monkeypatch):
    import backend.service as module
    repository = SQLiteRuntimeRepository(tmp_path / "changed-data-http.sqlite3")
    app = create_app(runtime_repository=repository)
    def changing_schema():
        raise module.DatasetChangedDuringReadError("private dataset path")
    monkeypatch.setattr(module, "stable_tool_schemas", changing_schema)
    client = TestClient(app)
    response = client.post("/v1/runs", json={"input": "offline"})
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail["code"] == "data_snapshot_changed"
    assert detail["retryable"] is True
    assert detail["stage"] == "preflight"
    assert detail["acceptance"] == "not_accepted"
    assert "private dataset path" not in response.text
    assert repository.list_runs() == []
    app.state.scheduler.shutdown()


def test_all_durable_commands_survive_postcommit_wake_failure(tmp_path, monkeypatch):
    import backend.service as module
    from datetime import datetime, timedelta, timezone
    monkeypatch.setattr(module, "RUN_RECOVERY_ENABLED", True)
    repository = SQLiteRuntimeRepository(tmp_path / "commands-http.sqlite3")
    app = create_app(runtime_repository=repository, owner_cookie_secret="command-owner")
    client = TestClient(app)
    client.get("/v1/history?session_id=empty")
    owner = owner_id_from_cookie(client.cookies.get("uca_owner"), "command-owner")
    pending = {"tool_call_id": "ask", "questions": [{"id": "budget", "prompt": "budget", "options": [], "allow_custom": True, "required": True}]}
    repository.create_run("answer-command", "answer-session", "offline", owner_id=owner)
    repository.pause_run("answer-command", checkpoint={"pending_input": pending}, pending_input=pending,
                         runtime={"status": "waiting_user"})
    repository.create_run("retry-command", "retry-session", "offline", owner_id=owner)
    repository.finish_run("retry-command", error="offline", failure_code="worker_error", retryable=True)
    repository.create_run("recover-command", "recover-session", "offline", owner_id=owner)
    lease = repository.acquire_run_lease("recover-command", "offline-worker", lease_ttl_s=30)
    repository.save_run_checkpoint("recover-command", {"checkpoint_version": 2}, lease_token=lease["token"])
    repository.reconcile_runs(now_iso=(datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat())
    def unavailable(*_args, **_kwargs):
        raise RuntimeError("offline wake failure")
    monkeypatch.setattr(app.state.scheduler, "submit", unavailable)
    monkeypatch.setattr(app.state.run_event_notifier, "notify", unavailable)
    monkeypatch.setattr(app.state.run_store, "load", unavailable)
    commands = [
        ("answer-command", "input", {"client_request_id": "answer-key", "tool_call_id": "ask", "resume_revision": 0,
                                      "answers": [{"question_id": "budget", "values": ["15"]}]}),
        ("retry-command", "retry", {"client_request_id": "retry-key"}),
        ("recover-command", "recover", {"client_request_id": "recover-key", "expected_version": repository.get_run("recover-command")["version"]}),
    ]
    for run_id, operation, payload in commands:
        response = client.post(f"/v1/runs/{run_id}/{operation}", json=payload)
        assert response.status_code == 202, response.text
        assert response.json()["run_id"]
        prior = repository.peek_client_request(owner, operation, payload["client_request_id"])
        assert prior is not None
    monkeypatch.undo()
    app.state.scheduler.shutdown(grace_seconds=0)


def test_public_token_and_durable_admission_are_atomic_and_idempotent(tmp_path):
    repository = SQLiteRuntimeRepository(tmp_path / "admission.sqlite3")
    release = threading.Event()

    def pending_runner(**_):
        # Keep the reservation active while checking admission. A default
        # offline model fails immediately and legitimately releases its slot.
        assert release.wait(10), "test did not release the pending run"
        return {"status": "done", "summary": "offline"}

    app = create_app(runtime_repository=repository, owner_cookie_secret="admission-owner",
                     runner=pending_runner,
                     public_access_token="test-deploy-token",
                     admission_limits={"global_pending": 1, "owner_pending": 1, "daily_tokens": 10000000})
    client = TestClient(app)
    assert client.get("/v1/history", params={"session_id": "x"}).status_code == 401
    headers = {"Authorization": "Bearer test-deploy-token"}
    payload = {"run_id": "admission-first", "client_request_id": "admission-key",
               "session_id": "admission-session", "input": "offline"}
    try:
        accepted = client.post("/v1/runs", json=payload, headers=headers)
        replay = client.post("/v1/runs", json=payload, headers=headers)
        rejected = client.post("/v1/runs", json={**payload, "run_id": "admission-second",
                                                 "client_request_id": "second-key"}, headers=headers)
        assert accepted.status_code == replay.status_code == 202
        assert accepted.json()["run_id"] == replay.json()["run_id"]
        assert rejected.status_code == 429
        with pytest.raises(KeyError):
            repository.get_run("admission-second")
    finally:
        release.set()
        app.state.scheduler.shutdown()


def test_daily_admission_converts_reservation_to_actual_usage(tmp_path):
    repository = SQLiteRuntimeRepository(tmp_path / "daily-admission.sqlite3")
    first = repository.create_run("daily-first", "daily-session-1", "q", owner_id="owner",
                                  admission_limits={"daily_tokens": 50}, reserve_tokens=30)
    assert first["status"] == "queued"
    repository.claim_start("daily-first")
    repository.finish_run("daily-first", result={"state": {"run_budget": {"total_tokens": 20}}},
                          error="offline", failure_code="worker_error")
    second = repository.create_run("daily-second", "daily-session-2", "q", owner_id="owner",
                                   admission_limits={"daily_tokens": 50}, reserve_tokens=30)
    assert second["status"] == "queued"


def test_daily_admission_reserves_the_full_logical_run_budget_once(tmp_path):
    repository = SQLiteRuntimeRepository(tmp_path / "daily-hard-reserve.sqlite3")
    budget = ContextBudget(max_context_tokens=100000, reserved_output_tokens=1000, safety_margin_tokens=1000,
                           min_recent_turns=0, max_single_tool_result_tokens=5000)
    app = create_app(runtime_repository=repository, owner_cookie_secret="daily-hard-reserve",
                     context_budget=budget, admission_limits={"daily_tokens": 1188000})
    client = TestClient(app)
    app.state.scheduler.submit = lambda *_args: None
    payload = {"run_id": "daily-reserve-one", "client_request_id": "daily-reserve-key",
               "session_id": "daily-reserve-session", "input": "q"}
    accepted = client.post("/v1/runs", json=payload)
    replay = client.post("/v1/runs", json=payload)
    rejected = client.post("/v1/runs", json={**payload, "run_id": "daily-reserve-two",
                                             "client_request_id": "daily-reserve-key-two"})
    assert accepted.status_code == replay.status_code == 202
    assert accepted.json()["run_id"] == replay.json()["run_id"]
    assert rejected.status_code == 429
    connection = repository._connect()
    try:
        row = connection.execute("SELECT reserved_tokens,actual_tokens FROM admission_usage WHERE run_id=?",
                                 ("daily-reserve-one",)).fetchone()
    finally:
        connection.close()
    assert (row["reserved_tokens"], row["actual_tokens"]) == (1188000, 0)


def test_daily_admission_reservation_uses_configured_model_call_budget(tmp_path, monkeypatch):
    import backend.service as module
    monkeypatch.setattr(module, "RUN_BUDGET_LIMITS", {**module.RUN_BUDGET_LIMITS, "model_calls": 24})
    repository = SQLiteRuntimeRepository(tmp_path / "configured-reserve.sqlite3")
    budget = ContextBudget(max_context_tokens=100000, reserved_output_tokens=1000, safety_margin_tokens=1000,
                           min_recent_turns=0, max_single_tool_result_tokens=5000)
    app = create_app(runtime_repository=repository, owner_cookie_secret="configured-reserve",
                     context_budget=budget, admission_limits={"daily_tokens": 1188000})
    app.state.scheduler.submit = lambda *_args: None
    response = TestClient(app).post("/v1/runs", json={"input": "q"})
    assert response.status_code == 429
    assert response.json()["detail"]["code"] == "admission_daily_token_limit"
    assert repository.list_runs() == []


def test_recovery_never_refunds_uncertain_lost_worker_usage(tmp_path):
    repository = SQLiteRuntimeRepository(tmp_path / "uncertain-usage.sqlite3")
    repository.create_run("uncertain", "uncertain-session", "q", owner_id="owner", reserve_tokens=30)
    with repository._transaction() as connection:
        repository._reserve_existing_admission(connection, "uncertain", "owner", {"daily_tokens": 100}, 20)
        repository._settle_admission_usage(connection, "uncertain", {"state": {"run_budget": {"total_tokens": 5}}})
        row = connection.execute("SELECT reserved_tokens,actual_tokens FROM admission_usage WHERE run_id='uncertain'").fetchone()
        assert (row["reserved_tokens"], row["actual_tokens"]) == (0, 35)
        with pytest.raises(ValueError, match="admission_daily_token_limit"):
            repository._check_admission(connection, "owner", {"daily_tokens": 40}, 10)


def test_sqlite_sse_status_cursor_reads_only_status_and_sequence(tmp_path):
    repository = SQLiteRuntimeRepository(tmp_path / "sse-cursor.sqlite3")
    repository.create_run("sse-cursor", "sse-session", "q", owner_id="owner")
    runs = SQLiteRunStore(repository)
    assert runs.status_cursor("sse-cursor") == ("queued", 0)
    runs.append("sse-cursor", {"event_type": "progress"})
    assert runs.status_cursor("sse-cursor") == ("queued", 1)
    assert runs.read_after("sse-cursor", 0, 200)[0]["event_type"] == "progress"
