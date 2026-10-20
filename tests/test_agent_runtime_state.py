import json

import pytest

from backend.agent_runtime.state import AgentState
from backend.agent_runtime.trace import RunTrace


def test_agent_state_starts_with_user_message_and_flags():
    state = AgentState.create("宝马3系多少钱", "你是二手车 Agent")

    assert [item["role"] for item in state.messages] == ["system", "user"]
    assert state.messages[1]["content"] == "宝马3系多少钱"
    assert state.flags["status"] == "initialized"
    assert state.flags["steps"] == 0
    assert state.trace_id


def test_agent_state_records_tool_result_and_is_json_serializable():
    state = AgentState.create("查几条车源", "系统约束")
    state.append_message(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "csv_query", "arguments": "{}"},
                }
            ],
        }
    )
    state.add_tool_result(
        "call-1",
        "csv_query",
        {"series": "宝马3系"},
        {"ok": True, "rows": [{"series": "宝马3系"}]},
        "done",
        12,
    )

    payload = state.to_dict()

    assert payload["messages"][-1]["role"] == "tool"
    assert payload["messages"][-1]["tool_call_id"] == "call-1"
    assert payload["messages"][-1]["name"] == "csv_query"
    assert payload["tool_results"][0]["tool_call_id"] == "call-1"
    assert payload["tool_results"][0]["tool_name"] == "csv_query"
    assert payload["tool_results"][0]["duration_ms"] == 12
    json.dumps(payload, ensure_ascii=False)


def test_agent_state_preserves_reasoning_content_for_model_context_only():
    state = AgentState.create("查几条车源", "系统约束")
    state.append_message(
        {
            "role": "assistant",
            "content": "",
            "reasoning_content": "先判断是否需要查询数据",
            "tool_calls": [],
        }
    )

    checkpoint = state.to_checkpoint()
    restored = AgentState.from_checkpoint(checkpoint)

    assert restored.messages[-1]["reasoning_content"] == "先判断是否需要查询数据"


def test_agent_state_rejects_invalid_message_role():
    state = AgentState.create("测试", "系统约束")

    with pytest.raises(ValueError, match="不支持的消息角色"):
        state.append_message({"role": "root", "content": "bad"})


def test_run_trace_records_events_and_sanitizes_non_json_values():
    trace = RunTrace.create("trace-1")
    trace.record("tool_execute", "tool", {"x": object()}, {"ok": True}, "done", 5,
                 constraint_revision=0)

    payload = trace.to_dict()

    assert payload["trace_id"] == "trace-1"
    assert payload["run_id"] == ""
    assert payload["segment_id"]
    assert payload["events"][0]["node"] == "tool_execute"
    assert payload["events"][0]["status"] == "done"
    assert payload["events"][0]["event_id"]
    assert payload["events"][0]["constraint_revision"] == 0
    assert "attempt" not in payload["events"][0]
    json.dumps(payload, ensure_ascii=False)


def test_model_trace_summarizes_request_bodies_without_changing_checkpoint_messages():
    from backend.agent_runtime import nodes

    state = AgentState.create("question", "system")
    state.messages.append({"role": "user", "content": "long private prompt"})
    trace = RunTrace.create("trace-summary")
    nodes._log(state, trace, {
        "node": "agent_execute", "event_type": "model_round",
        "input": {"model": "test", "messages": state.messages},
        "output": {"role": "assistant", "content": "x" * 700}, "status": "ok",
    }, 1)

    saved = state.to_checkpoint()
    assert saved["messages"] == state.messages
    assert isinstance(saved["run_logs"][0]["input"]["messages"], dict)
    assert "long private prompt" not in str(saved["run_logs"])
    assert saved["run_logs"][0]["output"]["content"]["bytes"] == 700
    assert "long private prompt" not in str(trace.to_dict())


def test_agent_state_checkpoint_round_trips_plan_and_pending_input():
    state = AgentState.create("帮我选车", "系统约束")
    state.plan = [{"id": "query", "title": "查询样本", "description": "先找可比车源", "tool_names": ["recommend"], "status": "running"}]
    ask_call = {
        "id": "call-ask",
        "type": "function",
        "function": {"name": "ask_user", "arguments": "{}"},
    }
    state.append_message({"role": "assistant", "content": "", "tool_calls": [ask_call]})
    state.pending_tool_batch = {
        "batch_id": "batch-ask",
        "assistant_message_index": len(state.messages) - 1,
        "calls": [ask_call],
        "next_index": 0,
        "completed_call_ids": [],
        "step": 1,
    }
    state.pending_input = {
        "questions": [
            {
                "id": "budget",
                "prompt": "预算上限是多少？",
                "options": [{"id": "a", "label": "15 万", "value": "15"}],
                "allow_custom": True,
                "required": True,
            }
        ],
        "tool_call_id": "call-ask",
        "step": 1,
        "batch_id": "batch-ask",
        "batch_index": 0,
    }
    state.runtime_status = "waiting_user"

    restored = AgentState.from_checkpoint(state.to_checkpoint())

    assert restored.messages == state.messages
    assert restored.plan == state.plan
    assert restored.pending_input == state.pending_input
    assert restored.runtime_status == "waiting_user"
    json.dumps(restored.to_checkpoint(), ensure_ascii=False)


def test_agent_state_rejects_checkpoint_with_unclosed_tool_batch_cursor():
    state = AgentState.create("帮我选车", "系统约束")
    call = {"id": "call-ask", "type": "function", "function": {"name": "ask_user", "arguments": "{}"}}
    state.append_message({"role": "assistant", "content": "", "tool_calls": [call]})
    state.pending_tool_batch = {
        "batch_id": "batch-ask", "assistant_message_index": len(state.messages) - 1,
        "calls": [call], "next_index": 1, "completed_call_ids": [], "step": 1,
    }

    with pytest.raises(ValueError, match="完成列表与游标不一致"):
        AgentState.from_checkpoint(state.to_checkpoint())


def test_agent_state_checkpoint_round_trips_answered_inputs():
    state = AgentState.create("帮我选车", "系统约束")
    state.answered_inputs = [
        {
            "question_id": "body",
            "prompt": "车型偏好？",
            "fingerprint": "body|车型偏好？",
            "value": "body_type=SUV",
            "tool_call_id": "call-ask",
        }
    ]

    restored = AgentState.from_checkpoint(state.to_checkpoint())

    assert restored.answered_inputs == state.answered_inputs


def test_agent_state_checkpoint_preserves_context_start_usage_and_full_messages():
    state = AgentState.create("当前问题", "系统约束", history=[{"role": "user", "content": "历史问题"}])
    state.flags["current_run_start"] = 2
    state.flags["token_usage"] = {"prompt_tokens": 17, "completion_tokens": 4, "total_tokens": 21, "model_calls": 1}
    state.append_message({"role": "assistant", "content": "", "reasoning_content": "续轮必需推理"})

    restored = AgentState.from_checkpoint(state.to_checkpoint())

    assert restored.flags["current_run_start"] == 2
    assert restored.flags["token_usage"]["total_tokens"] == 21
    assert restored.messages == state.messages
