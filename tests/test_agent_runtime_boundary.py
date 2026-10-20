import json

import pytest

from backend.agent_runtime.state import AgentState
from backend.agent_runtime.task_context import normalize_task_intent
from backend.agent_runtime.tool_registry import get_tool, tool_schemas
from backend.agent_runtime.tool_policy import Reject, preflight_tool_call
from backend.run_demo import _print_result


def test_registered_tools_exclude_rag_and_external_search():
    names = {item["function"]["name"].lower() for item in tool_schemas()}

    assert not any(
        token in name
        for name in names
        for token in ("kb", "embedding", "vector", "memory", "mcp", "web_search")
    )
    assert {"price_compare", "region_gap", "price_decay", "recommend", "csv_query"} <= names
    domain_tools = {"price_compare", "region_gap", "price_decay", "recommend", "csv_query"}
    assert all(get_tool(name).read_only for name in domain_tools)
    control_tools = {"ask_user", "declare_task", "set_plan", "update_plan", "finalize_answer"}
    assert control_tools <= names
    assert all(get_tool(name).effect == "agent_control" for name in control_tools)
    assert all(not get_tool(name).parallel_safe for name in control_tools)


def test_cli_result_exposes_trace_without_secret(capsys):
    result = {
        "input": "测试",
        "summary": "只展示工具结果",
        "summary_source": "agent",
        "trace_id": "trace-test",
        "state": {"flags": {"steps": 1, "successful_tool_count": 0}},
        "tool_results": [],
        "run_logs": [{"node": "finalize", "status": "ok"}],
    }

    _print_result(result)
    output = capsys.readouterr().out

    assert "trace-test" in output
    assert "执行步数：1" in output
    assert "sk-" not in json.dumps(result, ensure_ascii=False)


def test_cli_prints_csv_samples(capsys):
    _print_result(
        {
            "input": "代表性车源",
            "tool_results": [
                {
                    "tool_call_id": "call-csv",
                    "tool_name": "csv_query",
                    "input": {"series": "宝马3系", "limit": 1},
                    "status": "done",
                    "duration_ms": 2,
                    "ok": True,
                    "output": {
                        "ok": True,
                        "tool": "csv_query",
                        "match_count": 1,
                        "filters_applied": ["车系 = 宝马3系"],
                        "rows": [
                            {
                                "series": "宝马3系",
                                "city_name": "北京",
                                "production_year": 2022,
                                "price_wan": 18.0,
                                "mileage_km": 30000,
                            }
                        ],
                        "note": "展示前 1 条。",
                    },
                }
            ],
            "summary": "样本",
            "summary_source": "agent",
            "trace_id": "trace-csv",
            "state": {"flags": {}},
        }
    )
    output = capsys.readouterr().out

    assert "检索命中" in output
    assert "18.0 万" in output


def test_data_tool_is_blocked_until_task_intent_is_declared():
    state = AgentState.create("比较宝马3系", "system")
    call = {"id": "price-1", "function": {"name": "price_compare", "arguments": {"series": "宝马3系"}}}

    decision = preflight_tool_call(call, state)

    assert isinstance(decision, Reject)
    assert decision.error_code == "task_intent_required"


def test_multi_goal_task_requires_plan_and_plan_tool_binding():
    state = AgentState.create("比较并推荐", "system")
    state.task_intent = normalize_task_intent([
        {"id": "compare", "description": "比较两组价格", "deliverable": "价格对比"},
        {"id": "recommend", "description": "基于比较进行推荐", "deliverable": "推荐理由", "depends_on": ["compare"]},
    ])
    call = {"id": "price-1", "function": {"name": "price_compare", "arguments": {"series": "宝马3系"}}}

    missing_plan = preflight_tool_call(call, state)
    assert isinstance(missing_plan, Reject)
    assert missing_plan.error_code == "plan_required"

    state.plan = [{
        "id": "compare",
        "title": "比较价格",
        "description": "按目标车系查询价格分布",
        "tool_names": ["price_compare"],
        "goal_ids": ["compare"],
        "kind": "data",
        "completion_criteria": "tool_result",
        "status": "running",
    }, {
        "id": "recommend",
        "title": "生成推荐",
        "description": "根据已比较结果形成推荐",
        "tool_names": ["recommend"],
        "goal_ids": ["recommend"],
        "kind": "data",
        "completion_criteria": "tool_result",
        "status": "queued",
    }, {
        "id": "finalize",
        "title": "整理回答",
        "description": "覆盖所有目标并核验答案",
        "tool_names": ["finalize_answer"],
        "goal_ids": ["compare", "recommend"],
        "kind": "finalize",
        "completion_criteria": "validated_answer",
        "status": "queued",
    }]
    allowed = preflight_tool_call(call, state)
    assert not isinstance(allowed, Reject)

    wrong_tool = {"id": "csv-1", "function": {"name": "csv_query", "arguments": {"limit": 1}}}
    rejected = preflight_tool_call(wrong_tool, state)
    assert isinstance(rejected, Reject)
    assert rejected.error_code == "plan_tool_mismatch"


def test_single_goal_marked_as_multi_stage_is_blocked_until_plan_exists():
    state = AgentState.create("分析市场、核验样本并给出综合建议", "system")
    state.task_intent = normalize_task_intent([
        {"id": "report", "description": "分析市场并给出建议", "deliverable": "综合分析报告"},
    ], requires_plan=True)
    call = {"id": "price-1", "function": {"name": "price_compare", "arguments": {"series": "宝马3系"}}}

    decision = preflight_tool_call(call, state)

    assert isinstance(decision, Reject)
    assert decision.error_code == "plan_required"
    assert state.task_intent["mode"] == "planned"


def test_model_false_requires_plan_cannot_bypass_single_goal_plan_gate():
    state = AgentState.create("2022 年奥迪 A4L 价格", "system")
    # This is an old checkpoint shape: migrations may preserve the historical
    # false flag, but runtime policy must still gate every data tool on a plan.
    state.task_intent = {
        "revision": 1,
        "mode": "simple",
        "needs_plan": False,
        "goals": [{"id": "price", "description": "查询车系挂牌价", "deliverable": "价格分布"}],
    }
    call = {"id": "price-1", "function": {"name": "price_compare", "arguments": {"series": "奥迪 A4L"}}}

    decision = preflight_tool_call(call, state)

    assert decision.error_code == "plan_required"
    from backend.agent_runtime.loop import _sync_task_context_message

    _sync_task_context_message(state)
    assert "set_plan" in state.messages[-1]["content"]


@pytest.mark.parametrize("tool_name", ["ask_user", "load_skill", "declare_task"])
def test_control_success_cannot_satisfy_a_data_goal_at_finalize(tool_name, monkeypatch):
    from backend.agent_runtime.answer_validation import ValidationResult
    import backend.agent_runtime.tool_policy as policy

    monkeypatch.setattr(policy, "validate_finalize_answer", lambda *_args: ValidationResult(True, [], "已核验"))
    state = AgentState.create("查询挂牌价", "system")
    state.task_intent = normalize_task_intent([
        {"id": "price", "description": "查询挂牌价", "deliverable": "价格答案"},
    ])
    state.task_context = {"revision": 0}
    state.plan = [
        {
            "id": "query",
            "title": "查询价格",
            "description": "取得用户目标所需的价格数据",
            "tool_names": ["price_compare"],
            "goal_ids": ["price"],
            "kind": "data",
            "completion_criteria": "tool_result",
            "status": "complete",
        },
        {
            "id": "final",
            "title": "整理回答",
            "description": "引用已验证的价格证据",
            "tool_names": ["finalize_answer"],
            "goal_ids": ["price"],
            "kind": "finalize",
            "completion_criteria": "validated_answer",
            "status": "running",
        },
    ]
    state.tool_results = [{
        "tool_call_id": "control-result",
        "tool_name": tool_name,
        "status": "done",
        "plan_step_id": "query",
        "goal_ids": ["price"],
        "constraint_revision": 0,
        "output": {"ok": True, "control": "ask_user_response"},
    }]
    draft = {
        "schema_version": 2,
        "result_kind": "data_answer",
        "goal_ids": ["price"],
        "blocks": [{"kind": "fact_ref", "evidence_id": "ev1", "format": "sentence"}],
        "limitation_codes": [],
    }

    decision = preflight_tool_call(
        {"id": "finalize-1", "function": {"name": "finalize_answer", "arguments": draft}},
        state,
    )

    assert isinstance(decision, Reject)
    assert decision.error_code == "task_goal_without_data_result"


def test_no_data_answer_requires_a_current_bound_empty_data_result(monkeypatch):
    import backend.data_loader

    monkeypatch.setattr(backend.data_loader, "get_dataset_version", lambda: "dataset-v1")
    state = AgentState.create("查询挂牌价", "system")
    state.task_intent = normalize_task_intent([
        {"id": "price", "description": "查询挂牌价", "deliverable": "价格答案"},
    ])
    state.task_context = {"revision": 0}
    state.plan = [
        {
            "id": "query", "title": "查询价格", "description": "查询当前数据",
            "tool_names": ["price_compare"], "goal_ids": ["price"],
            "kind": "data", "completion_criteria": "tool_result", "status": "complete",
        },
        {
            "id": "final", "title": "说明结果", "description": "说明没有可用结果",
            "tool_names": ["finalize_answer"], "goal_ids": ["price"],
            "kind": "finalize", "completion_criteria": "validated_answer", "status": "running",
        },
    ]
    state.tool_results = [{
        "tool_call_id": "query-1", "tool_name": "price_compare", "status": "done",
        "plan_step_id": "query", "goal_ids": ["price"], "constraint_revision": 0,
        "dataset_version": "dataset-v1",
        "output": {"ok": True, "status": "empty", "result_status": "empty", "reason_code": "no_data"},
    }]
    draft = {
        "schema_version": 2, "result_kind": "no_data", "goal_ids": ["price"],
        "blocks": [{"kind": "text", "text": "当前条件下没有匹配数据。"}],
        "limitation_codes": [],
    }
    call = {"id": "finalize-1", "function": {"name": "finalize_answer", "arguments": draft}}

    assert not isinstance(preflight_tool_call(call, state), Reject)

    state.tool_results[0].pop("dataset_version")
    decision = preflight_tool_call(call, state)
    assert isinstance(decision, Reject)
    assert decision.error_code == "no_data_without_empty_result"


def test_completion_preserves_stale_evidence_as_the_failure_code():
    from backend.agent_runtime.completion_policy import decide_completion

    state = AgentState.create("查询挂牌价", "system")
    state.flags["answer_validation_failed"] = True
    state.flags["answer_validation"] = {
        "issues": [{"code": "stale_evidence", "category": "freshness"}],
    }

    outcome = decide_completion(
        state, {"summary": "答案没有通过校验。"}, execution_status="", report=None,
    )

    assert outcome.failure_code == "evidence_stale"


def test_empty_tool_dataset_version_survives_checkpoint_commit():
    from backend.agent_runtime.nodes import commit_tool_result

    state = AgentState.create("查询挂牌价", "system")
    commit_tool_result(
        state, "query-1", "price_compare", {"series": "宝马3系"},
        {"ok": True, "status": "empty", "result_status": "empty"}, "done", 5,
        plan_step_id="query", goal_ids=["price"], dataset_version="dataset-v1",
    )

    restored = AgentState.from_checkpoint(state.to_checkpoint())
    assert restored.tool_results[0]["dataset_version"] == "dataset-v1"


def test_finalize_v2_is_allowed_only_on_the_running_last_plan_step(monkeypatch):
    from backend.agent_runtime.answer_validation import ValidationResult
    import backend.agent_runtime.tool_policy as policy

    monkeypatch.setattr(policy, "validate_finalize_answer", lambda *_args: ValidationResult(True, [], "已核验"))
    state = AgentState.create("查询挂牌价", "system")
    state.task_intent = normalize_task_intent([
        {"id": "price", "description": "查询挂牌价", "deliverable": "价格答案"},
    ])
    state.task_context = {"revision": 0}
    state.plan = [
        {
            "id": "query", "title": "查询价格", "description": "取得挂牌价数据",
            "tool_names": ["price_compare"], "goal_ids": ["price"],
            "kind": "data", "completion_criteria": "tool_result", "status": "complete",
        },
        {
            "id": "final", "title": "整理回答", "description": "核验并提交答案",
            "tool_names": ["finalize_answer"], "goal_ids": ["price"],
            "kind": "finalize", "completion_criteria": "validated_answer", "status": "running",
        },
    ]
    state.tool_results = [{
        "tool_call_id": "query-1", "tool_name": "price_compare", "status": "done",
        "plan_step_id": "query", "goal_ids": ["price"], "constraint_revision": 0,
        "output": {"ok": True},
    }]
    from backend.data_loader import get_dataset_version
    state.evidence_records = [{
        "evidence_id": "ev1", "tool_call_id": "query-1", "goal_ids": ["price"],
        "constraint_revision": 0, "dataset_version": get_dataset_version(),
        "metric": "price.median_yuan", "value": 200000, "unit": "CNY",
    }]
    draft = {
        "schema_version": 2,
        "result_kind": "data_answer",
        "goal_ids": ["price"],
        "blocks": [{"kind": "fact_ref", "evidence_id": "ev1", "format": "sentence"}],
        "limitation_codes": [],
    }
    call = {"id": "finalize-1", "function": {"name": "finalize_answer", "arguments": draft}}

    assert not isinstance(preflight_tool_call(call, state), Reject)

    state.plan[-1]["status"] = "queued"
    decision = preflight_tool_call(call, state)
    assert isinstance(decision, Reject)
    assert decision.error_code == "finalize_step_required"


def test_validated_answer_can_close_running_finalize_step_in_runtime():
    from backend.agent_runtime.completion_policy import plan_ready_for_completion

    state = AgentState.create("查询挂牌价", "system")
    state.task_intent = normalize_task_intent([
        {"id": "price", "description": "查询挂牌价", "deliverable": "价格答案"},
    ])
    state.task_context = {"revision": 0}
    state.plan = [
        {
            "id": "query", "title": "查询价格", "description": "取得挂牌价数据",
            "tool_names": ["price_compare"], "goal_ids": ["price"],
            "kind": "data", "completion_criteria": "tool_result", "status": "complete",
        },
        {
            "id": "final", "title": "整理回答", "description": "核验并提交答案",
            "tool_names": ["finalize_answer"], "goal_ids": ["price"],
            "kind": "finalize", "completion_criteria": "validated_answer", "status": "running",
        },
    ]
    state.flags.update(answer_finalized=True, validated_answer="已核验答案", task_completed=True)
    state.tool_results = [{
        "tool_call_id": "finalize-1", "tool_name": "finalize_answer", "status": "done",
        "plan_step_id": "final", "goal_ids": ["price"], "constraint_revision": 0,
        "output": {"ok": True, "answer_validation": {"valid": True}},
    }]

    assert plan_ready_for_completion(state) is True


def _unsupported_running_data_state():
    state = AgentState.create("15 万以内的 SUV 推荐", "system")
    state.task_intent = normalize_task_intent([
        {"id": "recommendation", "description": "按条件推荐车型", "deliverable": "候选车型"},
    ])
    state.task_context = {"revision": 0}
    state.plan = [
        {
            "id": "query", "title": "查询候选", "description": "按用户条件检索候选",
            "tool_names": ["recommend"], "goal_ids": ["recommendation"],
            "kind": "data", "completion_criteria": "tool_result", "status": "running",
        },
        {
            "id": "final", "title": "整理结果", "description": "核验并回答",
            "tool_names": ["finalize_answer"], "goal_ids": ["recommendation"],
            "kind": "finalize", "completion_criteria": "validated_answer", "status": "queued",
        },
    ]
    state.tool_results = [{
        "tool_call_id": "recommend-failed", "tool_name": "recommend", "status": "error",
        "plan_step_id": "query", "goal_ids": ["recommendation"], "constraint_revision": 0,
        "output": {"ok": False, "reason_code": "unsupported_filter", "error_code": "unsupported_filter"},
    }]
    state.flags["pending_user_decision"] = {
        "reason_code": "unsupported_filter",
        "tool_name": "recommend",
        "tool_call_id": "recommend-failed",
        "plan_step_id": "query",
        "goal_ids": ["recommendation"],
        "constraint_revision": 0,
    }
    return state


def test_unsupported_filter_recovery_question_must_match_failed_call_step_and_goals():
    from backend.agent_runtime.tool_policy import preflight_tool_call

    question = {
        "questions": [{
            "id": "filter", "prompt": "是否移除不支持的车型类别条件？",
            "options": [{"id": "remove", "label": "移除", "value": "remove"}],
            "allow_custom": False,
        }],
    }
    call = {"id": "ask-recovery", "function": {"name": "ask_user", "arguments": question}}

    assert not isinstance(preflight_tool_call(call, _unsupported_running_data_state()), Reject)

    for field, value in [
        ("tool_call_id", "other-call"),
        ("plan_step_id", "other-step"),
        ("goal_ids", ["other-goal"]),
    ]:
        state = _unsupported_running_data_state()
        state.flags["pending_user_decision"][field] = value
        decision = preflight_tool_call(call, state)
        assert isinstance(decision, Reject)
        assert decision.error_code == "clarification_step_required"


def test_unsupported_filter_answer_does_not_complete_data_and_retry_is_required(monkeypatch):
    from backend.agent_runtime import nodes
    from backend.agent_runtime.trace import RunTrace
    from backend.agent_runtime.tool_policy import preflight_tool_call

    state = _unsupported_running_data_state()
    trace = RunTrace.create(state.trace_id)
    real_execute = nodes.execute_registered_tool
    monkeypatch.setattr(nodes, "execute_registered_tool", lambda name, args: real_execute(name, args))
    question = {
        "questions": [{
            "id": "filter", "prompt": "是否移除不支持的车型类别条件？",
            "options": [{"id": "remove", "label": "移除", "value": "remove"}],
            "allow_custom": False,
        }],
    }
    ask_call = {"id": "ask-recovery", "function": {"name": "ask_user", "arguments": question}}

    waiting = nodes.tool_execute_node(state, trace, [ask_call], step=1)

    assert waiting == {"status": "waiting_user", "tool_call_id": "ask-recovery"}
    assert state.pending_input["tool_call_id"] == "ask-recovery"
    assert state.pending_input["caused_by_tool_call_id"] == "recommend-failed"
    assert state.pending_input["plan_step_id"] == "query"
    assert state.pending_input["goal_ids"] == ["recommendation"]

    completion = nodes.append_user_answers(
        state,
        [{"question_id": "filter", "value": "remove"}],
    )

    assert completion["output"]["caused_by_tool_call_id"] == "recommend-failed"
    assert state.plan[0]["status"] == "running"
    assert state.tool_results[-1]["tool_name"] == "ask_user"
    blocked_complete = preflight_tool_call(
        {"id": "complete-query", "function": {"name": "update_plan", "arguments": {
            "updates": [{"id": "query", "status": "complete"}],
        }}},
        state,
    )
    assert isinstance(blocked_complete, Reject)
    assert blocked_complete.error_code == "plan_step_result_required"

    retry = preflight_tool_call(
        {"id": "recommend-retry", "function": {"name": "recommend", "arguments": {"budget_max_wan": 15}}},
        state,
    )
    assert not isinstance(retry, Reject)
