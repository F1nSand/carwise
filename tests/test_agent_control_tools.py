import pytest

from backend.agent_runtime.control_tools import (
    apply_plan_updates,
    initialize_plan,
    normalize_plan,
    normalize_questions,
    question_fingerprint,
    validate_answers,
)
from backend.agent_runtime.task_context import normalize_task_intent
from backend.agent_runtime.tool_registry import execute_registered_tool, tool_schemas


def test_control_tools_are_exposed_with_strict_nested_schemas():
    names = {item["function"]["name"] for item in tool_schemas()}

    assert {"ask_user", "declare_task", "set_plan", "update_plan"}.issubset(names)
    declare_schema = next(item["function"]["parameters"] for item in tool_schemas() if item["function"]["name"] == "declare_task")
    assert "requires_plan" in declare_schema["required"]
    assert declare_schema["properties"]["requires_plan"]["type"] == "boolean"
    plan_schema = next(item["function"]["parameters"] for item in tool_schemas() if item["function"]["name"] == "set_plan")
    step_schema = plan_schema["properties"]["steps"]["items"]
    assert {"kind", "completion_criteria"} <= set(step_schema["required"])
    finalize_schema = next(item["function"]["parameters"] for item in tool_schemas() if item["function"]["name"] == "finalize_answer")
    assert finalize_schema["properties"]["schema_version"]["const"] == 2
    assert set(finalize_schema["required"]) == {
        "schema_version", "result_kind", "goal_ids", "blocks", "limitation_codes",
    }
    assert {"text", "fact_ref", "recommendation_table"} == {
        block["properties"]["kind"]["const"]
        for block in finalize_schema["properties"]["blocks"]["items"]["oneOf"]
    }


def test_task_intent_derives_plan_mode_from_multiple_goals_and_dependencies():
    simple = normalize_task_intent([
        {"id": "query", "description": "查询宝马3系价格", "deliverable": "价格分布"},
    ])
    multi = normalize_task_intent([
        {"id": "compare", "description": "比较两款车的价格", "deliverable": "价格对比"},
        {"id": "recommend", "description": "按对比结果推荐", "deliverable": "推荐理由", "depends_on": ["compare"]},
    ])

    assert simple["needs_plan"] is True
    assert simple["mode"] == "planned"
    assert simple["complexity"] == "single_goal"
    assert multi["needs_plan"] is True
    staged_single_goal = normalize_task_intent([
        {"id": "report", "description": "输出一份综合报告", "deliverable": "综合报告"},
    ], requires_plan=True)
    assert staged_single_goal["mode"] == "planned"
    assert staged_single_goal["needs_plan"] is True
    assert multi["mode"] == "planned"
    with pytest.raises(ValueError, match="requires_plan 必须是布尔值"):
        normalize_task_intent(
            [{"id": "bad", "description": "无效类型", "deliverable": "错误"}],
            requires_plan="yes",
        )


@pytest.mark.parametrize(
    "goals, message",
    [
        ([{"id": "same", "description": "A", "deliverable": "A"}, {"id": "same", "description": "B", "deliverable": "B"}], "目标 id 不能重复"),
        ([{"id": "a", "description": "A", "deliverable": "A", "depends_on": ["missing"]}], "未知依赖"),
        ([{"id": "a", "description": "A", "deliverable": "A", "depends_on": ["b"]}, {"id": "b", "description": "B", "deliverable": "B", "depends_on": ["a"]}], "不能形成循环"),
    ],
)
def test_task_intent_rejects_duplicate_unknown_or_cyclic_goals(goals, message):
    with pytest.raises(ValueError, match=message):
        normalize_task_intent(goals)


def test_ask_user_normalizes_ordered_questions_and_custom_input():
    result = execute_registered_tool(
        "ask_user",
        {
            "questions": [
                {
                    "id": "budget",
                    "prompt": "预算上限是多少？",
                    "options": [
                        {"id": "a", "label": "15 万以内", "value": "15"},
                        {"id": "b", "label": "20 万以内", "value": "20"},
                    ],
                    "allow_custom": True,
                    "required": True,
                }
            ]
        },
    )

    assert result["ok"] is True
    assert result["control"] == "ask_user"
    assert result["questions"][0]["id"] == "budget"
    assert result["questions"][0]["allow_custom"] is True


@pytest.mark.parametrize(
    "questions, message",
    [
        (
            [
                {"id": "same", "prompt": "问题 1", "options": [], "allow_custom": True},
                {"id": "same", "prompt": "问题 2", "options": [], "allow_custom": True},
            ],
            "问题 id 不能重复",
        ),
        (
            [{"id": str(index), "prompt": "问题", "options": [], "allow_custom": True} for index in range(9)],
            "最多同时提出 8 个问题",
        ),
    ],
)
def test_ask_user_rejects_unsafe_question_shapes(questions, message):
    result = execute_registered_tool("ask_user", {"questions": questions})

    assert result["ok"] is False
    assert message in result["error"]


def test_answer_validation_rejects_unknown_ids_missing_required_and_long_values():
    questions = normalize_questions(
        [
            {
                "id": "budget",
                "prompt": "预算？",
                "options": [],
                "allow_custom": True,
                "required": True,
            },
            {
                "id": "city",
                "prompt": "城市？",
                "options": [],
                "allow_custom": True,
                "required": False,
            },
        ]
    )

    from backend.agent_runtime.control_tools import validate_answers

    with pytest.raises(ValueError, match="未知问题 id"):
        validate_answers(questions, [{"question_id": "unknown", "value": "x"}])
    with pytest.raises(ValueError, match="缺少必答问题"):
        validate_answers(questions, [])
    with pytest.raises(ValueError, match="500"):
        validate_answers(questions, [{"question_id": "budget", "value": "x" * 501}])


def test_set_plan_rejects_duplicate_step_ids_and_normalizes_tool_names():
    result = execute_registered_tool(
        "set_plan",
        {
            "steps": [
                {"id": "inspect", "title": "检查", "description": "先检查数据", "tool_names": ["csv_query"], "goal_ids": ["task"], "kind": "data", "completion_criteria": "tool_result"},
                {"id": "inspect", "title": "重复", "description": "不能重复", "tool_names": ["csv_query"], "goal_ids": ["task"], "kind": "data", "completion_criteria": "tool_result"},
                {"id": "finalize", "title": "回答", "description": "整理答案", "tool_names": ["finalize_answer"], "goal_ids": ["task"], "kind": "finalize", "completion_criteria": "validated_answer"},
            ]
        },
    )

    assert result["ok"] is False
    assert "步骤 id 不能重复" in result["error"]
    assert normalize_plan([
        {"id": "inspect", "title": "检查", "description": "先检查数据", "tool_names": ["csv_query"], "goal_ids": ["task"], "kind": "data", "completion_criteria": "tool_result"},
        {"id": "finalize", "title": "回答", "description": "整理答案", "tool_names": ["finalize_answer"], "goal_ids": ["task"], "kind": "finalize", "completion_criteria": "validated_answer"},
    ])[0]["tool_names"] == ["csv_query"]

    with pytest.raises(ValueError, match="必须同时提供 kind 和 completion_criteria"):
        normalize_plan([
            {"id": "query", "title": "查询", "description": "查询车源", "tool_names": ["csv_query"], "goal_ids": ["task"], "kind": "data"},
            {"id": "final", "title": "回答", "description": "整理答案", "tool_names": ["finalize_answer"], "goal_ids": ["task"], "kind": "finalize", "completion_criteria": "validated_answer"},
        ])


def test_question_fingerprint_uses_decision_content_not_model_question_id():
    first = {
        "id": "body-a",
        "prompt": "车型偏好？",
        "options": [{"id": "suv", "label": "SUV", "value": "body_type=SUV"}],
        "allow_custom": True,
        "required": True,
    }
    same_content = {**first, "id": "body-b"}
    revised = {**first, "prompt": "SUV 条件无法生效，你希望怎么继续？"}

    assert question_fingerprint(first) == question_fingerprint(same_content)
    assert question_fingerprint(first) != question_fingerprint(revised)


def test_plan_state_transitions_are_linear_and_definitions_are_immutable():
    plan = initialize_plan(
        [
            {"id": "inspect", "title": "检查", "description": "先检查数据", "tool_names": ["recommend"], "goal_ids": ["task"], "kind": "data", "completion_criteria": "tool_result"},
            {"id": "report", "title": "补充查询", "description": "按需要补充样本查询", "tool_names": ["csv_query"], "goal_ids": ["task"], "kind": "data", "completion_criteria": "tool_result"},
            {"id": "publish", "title": "整理回答", "description": "核验并提交答案", "tool_names": ["finalize_answer"], "goal_ids": ["task"], "kind": "finalize", "completion_criteria": "validated_answer"},
        ]
    )

    with pytest.raises(ValueError, match="只能有一个 running"):
        apply_plan_updates(plan, [{"id": "report", "status": "running"}])
    with pytest.raises(ValueError, match="未知计划步骤"):
        apply_plan_updates(plan, [{"id": "missing", "status": "running"}])
    with pytest.raises(ValueError, match="只能有一个 running"):
        apply_plan_updates(plan, [{"id": "inspect", "status": "running"}, {"id": "report", "status": "running"}])

    completed = apply_plan_updates(plan, [{"id": "inspect", "status": "complete"}])
    advanced = apply_plan_updates(completed, [{"id": "report", "status": "running"}])
    assert [step["status"] for step in advanced] == ["complete", "running", "queued"]
    assert advanced[0]["title"] == "检查"
    with pytest.raises(ValueError, match="前置步骤尚未完成"):
        apply_plan_updates(completed, [{"id": "publish", "status": "running"}])
    with pytest.raises(ValueError, match="不能回退"):
        apply_plan_updates(advanced, [{"id": "report", "status": "queued"}])


def test_skipped_plan_step_closes_its_dependency_for_following_steps():
    queued = normalize_plan(
        [
            {"id": "optional", "title": "可选检查", "description": "仅在条件支持时执行", "tool_names": ["csv_query"], "goal_ids": ["task"], "kind": "data", "completion_criteria": "tool_result"},
            {"id": "report", "title": "汇总", "description": "核验并提交答案", "tool_names": ["finalize_answer"], "goal_ids": ["task"], "kind": "finalize", "completion_criteria": "validated_answer"},
        ]
    )

    skipped = apply_plan_updates(queued, [{"id": "optional", "status": "skipped", "reason": "当前数据不支持"}])
    running = apply_plan_updates(skipped, [{"id": "report", "status": "running"}])

    assert [step["status"] for step in running] == ["skipped", "running"]


def test_plan_revision_can_edit_only_queued_steps_and_keeps_completed_history():
    plan = initialize_plan([
        {"id": "query_a", "title": "查询 A", "description": "查询目标 A", "tool_names": ["price_compare"], "goal_ids": ["a"], "kind": "data", "completion_criteria": "tool_result"},
        {"id": "query_b", "title": "查询 B", "description": "查询目标 B", "tool_names": ["price_compare"], "goal_ids": ["b"], "kind": "data", "completion_criteria": "tool_result"},
        {"id": "final", "title": "整理回答", "description": "提交所有目标", "tool_names": ["finalize_answer"], "goal_ids": ["a", "b"], "kind": "finalize", "completion_criteria": "validated_answer"},
    ])
    changed = apply_plan_updates(plan, [{
        "id": "query_b",
        "title": "按补充条件查询 B",
        "description": "依据已确认的新条件查询目标 B",
        "reason": "用户追加了查询条件",
    }])

    assert changed[0] == plan[0]
    assert changed[1]["title"] == "按补充条件查询 B"
    assert changed[1]["revision_reason"] == "用户追加了查询条件"
    with pytest.raises(ValueError, match="queued"):
        apply_plan_updates(plan, [{
            "id": "query_a",
            "title": "重写已运行步骤",
            "reason": "不允许改写",
        }])


def test_multiple_question_answers_are_normalized_to_values():
    questions = normalize_questions(
        [
            {
                "id": "brands",
                "prompt": "品牌？",
                "options": [
                    {"id": "a", "label": "哈弗", "value": "哈弗"},
                    {"id": "b", "label": "长安", "value": "长安"},
                ],
                "allow_custom": True,
                "selection_mode": "multiple",
                "max_selections": 3,
            }
        ]
    )
    normalized = validate_answers(
        questions,
        [{"question_id": "brands", "values": ["哈弗", "长安", "哈弗"]}],
    )
    assert normalized == [{"question_id": "brands", "values": ["哈弗", "长安"]}]
    with pytest.raises(ValueError, match="最多选择"):
        validate_answers(questions, [{"question_id": "brands", "values": ["哈弗", "长安", "吉利", "比亚迪"]}])
