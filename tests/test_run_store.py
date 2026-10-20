import json
import time

from backend.agent_runtime.run_store import RunStore
from backend.api_projection import public_run_event


def test_run_store_assigns_monotonic_sequence_and_replays_after_cursor(tmp_path):
    store = RunStore(tmp_path / "runs")
    store.create("run-1", "session-1", "预算问题")
    store.start("run-1")
    first = store.append("run-1", {"event_type": "model_round", "step": 1})
    second = store.append("run-1", {"event_type": "final", "content": "完成"})
    store.finish("run-1", {"summary": "完成"})

    assert first["seq"] < second["seq"]
    assert [item["seq"] for item in store.read_after("run-1", first["seq"])] == [second["seq"], second["seq"] + 1]
    snapshot = store.load("run-1")
    assert snapshot["status"] == "done"
    assert json.dumps(snapshot, ensure_ascii=False)


def test_run_store_create_is_idempotent(tmp_path):
    store = RunStore(tmp_path / "runs")
    first = store.create("run-2", "session-2", "同一个请求")
    second = store.create("run-2", "session-2", "同一个请求")
    assert first["run_id"] == second["run_id"] == "run-2"
    assert store.read_after("run-2") == []


def test_list_filters_by_session_and_sorts_latest_first(tmp_path):
    store = RunStore(tmp_path / "runs")
    store.create("run-old", "session-one", "旧问题")
    store.finish("run-old", result={"summary": "旧"})
    time.sleep(0.01)
    store.create("run-new", "session-one", "新问题")
    store.finish("run-new", result={"summary": "新"})
    store.create("run-other", "session-two", "其他问题")
    store.finish("run-other", result={"summary": "其他"})

    assert [item["run_id"] for item in store.list("session-one")] == ["run-new", "run-old"]
    assert [item["run_id"] for item in store.list("session-two")] == ["run-other"]
    assert store.list("missing") == []


def test_run_store_persists_waiting_checkpoint_and_keeps_event_sequence(tmp_path):
    store = RunStore(tmp_path / "runs")
    store.create("run-wait", "session-wait", "帮我选车", context_id="context-wait")
    store.start("run-wait")
    first = store.append("run-wait", {"event_type": "input_required", "step": 1})
    waiting = store.pause(
        "run-wait",
        checkpoint={"trace_id": "trace-wait", "pending_input": {"tool_call_id": "ask-budget", "questions": [{"id": "budget"}]}},
        pending_input={"tool_call_id": "ask-budget", "questions": [{"id": "budget"}]},
        runtime={"status": "waiting_user"},
    )

    assert waiting["status"] == "waiting_user"
    assert waiting["context_id"] == "context-wait"
    assert waiting["pending_input"]["questions"][0]["id"] == "budget"
    assert waiting["checkpoint"]["trace_id"] == "trace-wait"
    assert store.claim_waiting("run-wait") is True
    claimed = store.load("run-wait")
    assert claimed["status"] == "running"
    assert claimed["resume_revision"] == 1
    assert store.claim_waiting("run-wait") is False
    assert store.read_after("run-wait", 0)[0]["event_type"] == "input_required"


def test_claim_waiting_requires_current_tool_call_and_revision(tmp_path):
    store = RunStore(tmp_path / "runs")
    store.create("run-revision", "session-revision", "问题")
    store.start("run-revision")
    store.pause(
        "run-revision",
        checkpoint={"pending_input": {"tool_call_id": "call-a", "questions": [{"id": "budget"}]}},
        pending_input={"tool_call_id": "call-a", "questions": [{"id": "budget"}]},
    )

    assert store.claim_waiting("run-revision", expected_tool_call_id="call-old") is False
    assert store.claim_waiting("run-revision", expected_tool_call_id="call-a", expected_revision=1) is False
    assert store.claim_waiting("run-revision", expected_tool_call_id="call-a", expected_revision=0) is True
    assert store.load("run-revision")["resume_revision"] == 1


def test_finish_emits_compact_terminal_result_without_runtime_internals(tmp_path):
    store = RunStore(tmp_path / "runs")
    store.create("run-compact", "session-compact", "测试终态")
    store.start("run-compact")
    result = {
        "summary": "完成",
        "summary_source": "agent",
        "final_message": {"role": "assistant", "content": "最终答案"},
        "report": {"id": "report-compact", "summaryText": "结论"},
        "run_id": "run-compact",
        "context_id": "context-compact",
        "confirmed_filters": {"budget_max_wan": 15},
        "state": {"large": "x" * 500_000},
        "trace": ["trace-entry"],
        "run_logs": [{"tool": "recommend", "result": "x" * 10_000}],
        "tool_results": [{"result": "x" * 10_000}],
        "tool_schemas": [{"name": "recommend"}],
        "facts": {"internal": True},
    }

    snapshot = store.finish("run-compact", result=result)
    terminal_event = store.read_after("run-compact")[-1]

    assert snapshot["result"]["state"]["large"] == "x" * 500_000
    assert terminal_event["event_type"] == "done"
    assert terminal_event["result"]["report"]["id"] == "report-compact"
    assert terminal_event["result"]["final_message"]["content"] == "最终答案"
    assert terminal_event["result"]["confirmed_filters"] == {"budget_max_wan": 15}
    assert not any(
        key in terminal_event["result"]
        for key in ("state", "trace", "run_logs", "tool_results", "tool_schemas", "facts")
    )
    assert len(json.dumps(terminal_event, ensure_ascii=False).encode("utf-8")) < 64 * 1024


def test_json_run_store_terminal_error_preserves_public_failure_code(tmp_path):
    store = RunStore(tmp_path / "runs")
    store.create("run-model-timeout", "session-timeout", "查询")
    store.start("run-model-timeout")

    snapshot = store.finish(
        "run-model-timeout",
        error="模型响应超时。",
        failure_code="model_timeout",
        retryable=True,
    )
    event = public_run_event(store.read_after("run-model-timeout")[-1])

    assert snapshot["failure_code"] == "model_timeout"
    assert snapshot["retryable"] is True
    assert event["failure_code"] == "model_timeout"
    assert event["retryable"] is True
