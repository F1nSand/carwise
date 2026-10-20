import pytest

from backend.agent_runtime.nodes import tool_execute_node
from backend.agent_runtime.run_budget import new_run_budget
from backend.agent_runtime.state import AgentState, CheckpointMigrationRequired
from backend.agent_runtime.task_context import legacy_task_intent
from backend.agent_runtime.trace import RunTrace


def test_legacy_checkpoint_derives_pending_tool_batch_for_waiting_user():
    checkpoint = {
        "checkpoint_version": 1,
        "user_text": "帮我选车",
        "trace_id": "trace-legacy",
        "messages": [
            {"role": "system", "content": "system"},
            {"role": "assistant", "content": "", "tool_calls": [{
                "id": "ask-budget", "type": "function",
                "function": {"name": "ask_user", "arguments": "{}"},
            }]},
        ],
        "pending_input": {"tool_call_id": "ask-budget", "questions": [{"id": "budget"}]},
    }

    restored = AgentState.from_checkpoint(checkpoint)

    assert restored.pending_tool_batch["batch_id"] == "legacy-1"
    assert restored.pending_tool_batch["next_index"] == 0
    assert restored.pending_input["batch_index"] == 0
    assert restored.task_intent["source"] == "checkpoint_compat"
    assert restored.task_intent["mode"] == "planned"
    assert restored.task_intent["needs_plan"] is True
    assert restored.task_intent["legacy_synthetic_goal"] is True


def test_legacy_simple_checkpoint_can_replace_placeholder_and_publish_plan_without_replan_budget():
    state = AgentState.create("查询宝马 3 系价格", "system")
    state.task_intent = legacy_task_intent(state.user_text)
    state.run_budget = new_run_budget({"replans": 0})
    goals = [{
        "id": "price_query",
        "description": "查询宝马 3 系价格分布",
        "deliverable": "挂牌价分布和样本量",
    }]
    calls = [
        {
            "id": "declare-legacy-resume",
            "type": "function",
            "function": {
                "name": "declare_task",
                "arguments": {"goals": goals, "requires_plan": False},
            },
        },
        {
            "id": "plan-legacy-resume",
            "type": "function",
            "function": {
                "name": "set_plan",
                "arguments": {"steps": [
                    {
                        "id": "query_price",
                        "title": "查询宝马 3 系价格",
                        "description": "读取符合用户条件的挂牌价统计和样本量",
                        "tool_names": ["price_compare"],
                        "goal_ids": ["price_query"],
                        "kind": "data",
                        "completion_criteria": "tool_result",
                    },
                    {
                        "id": "publish_answer",
                        "title": "整理并核验回答",
                        "description": "根据本次查询证据整理可读答案",
                        "tool_names": ["finalize_answer"],
                        "goal_ids": ["price_query"],
                        "kind": "finalize",
                        "completion_criteria": "validated_answer",
                    },
                ]},
            },
        },
    ]

    tool_execute_node(state, RunTrace.create(state.trace_id), calls, step=1)

    assert [goal["id"] for goal in state.task_intent["goals"]] == ["price_query"]
    assert "legacy_synthetic_goal" not in state.task_intent
    assert state.flags["legacy_task_intent_replaced"] is True
    assert "pending_plan_revision" not in state.flags
    assert state.run_budget["replans"] == 0
    assert [item["status"] for item in state.tool_results] == ["done", "done"]
    assert [step["status"] for step in state.plan] == ["running", "queued"]


def test_legacy_checkpoint_preserves_plan_and_attaches_compatibility_goal():
    checkpoint = {
        "checkpoint_version": 2,
        "user_text": "比较车型后推荐",
        "trace_id": "trace-legacy-plan",
        "messages": [{"role": "system", "content": "system"}],
        "plan": [
            {"id": "compare", "title": "比较价格", "description": "查询价格分布", "tool_names": ["price_compare"], "status": "running"},
            {"id": "recommend", "title": "推荐", "description": "根据对比推荐", "tool_names": ["recommend"], "status": "queued"},
        ],
    }

    restored = AgentState.from_checkpoint(checkpoint)

    assert restored.task_intent["source"] == "checkpoint_compat"
    assert restored.task_intent["needs_plan"] is True
    assert restored.plan[0]["status"] == "running"
    assert restored.plan[0]["goal_ids"] == ["legacy_task"]


def test_legacy_waiting_checkpoint_without_ask_call_requires_migration():
    checkpoint = {
        "checkpoint_version": 1,
        "user_text": "帮我选车",
        "trace_id": "trace-legacy-invalid",
        "messages": [
            {"role": "system", "content": "system"},
            {"role": "assistant", "content": "thinking"},
        ],
        "pending_input": {"tool_call_id": "ask-budget", "questions": [{"id": "budget"}]},
    }

    with pytest.raises(CheckpointMigrationRequired, match="无法安全迁移"):
        AgentState.from_checkpoint(checkpoint)


def test_checkpoint_from_unknown_future_version_requires_migration():
    checkpoint = {
        "checkpoint_version": 5,
        "user_text": "帮我选车",
        "trace_id": "trace-future",
        "messages": [{"role": "system", "content": "system"}],
    }

    with pytest.raises(CheckpointMigrationRequired, match="不支持的 checkpoint 版本"):
        AgentState.from_checkpoint(checkpoint)


def test_v3_task_intent_is_preserved_when_checkpoint_version_is_upgraded():
    task_intent = {
        "revision": 3,
        "mode": "simple",
        "needs_plan": False,
        "goals": [{"id": "legacy", "description": "旧目标", "deliverable": "说明", "depends_on": []}],
    }
    checkpoint = {
        "checkpoint_version": 3,
        "user_text": "继续旧任务",
        "trace_id": "trace-v3-intent",
        "messages": [{"role": "system", "content": "system"}, {"role": "user", "content": "继续旧任务"}],
        "task_intent": task_intent,
    }

    restored = AgentState.from_checkpoint(checkpoint)

    assert restored.task_intent == task_intent


def test_v3_running_plan_gets_metadata_and_runtime_finalize_without_losing_completed_steps():
    task_intent = {
        "revision": 3,
        "mode": "simple",
        "needs_plan": False,
        "goals": [{"id": "legacy", "description": "查价格", "deliverable": "价格分布", "depends_on": []}],
    }
    checkpoint = {
        "checkpoint_version": 3,
        "user_text": "继续旧任务",
        "trace_id": "trace-v3-plan",
        "messages": [{"role": "system", "content": "system"}, {"role": "user", "content": "继续旧任务"}],
        "flags": {"plan_revision": 2},
        "task_intent": task_intent,
        "plan": [{
            "id": "query-price", "title": "查询价格", "description": "读取挂牌价统计",
            "tool_names": ["price_compare"], "goal_ids": ["legacy"], "status": "complete",
        }],
    }

    restored = AgentState.from_checkpoint(checkpoint)

    assert restored.plan[0]["status"] == "complete"
    assert restored.plan[0]["kind"] == "data"
    assert restored.plan[0]["completion_criteria"] == "tool_result"
    assert restored.plan[-1]["kind"] == "finalize"
    assert restored.plan[-1]["status"] == "running"
    assert restored.plan[-1]["goal_ids"] == ["legacy"]
    assert restored.task_intent == task_intent
    assert restored.flags["plan_revision"] == 3
    assert restored.flags["plan_change_reason"] == "runtime_migration"
