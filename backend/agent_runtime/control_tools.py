"""Agent 与前端交互用的受控工具。

这些工具不访问数据，也不决定业务分支；它们只把 Agent 的计划和待确认问题
转换成受约束的 JSON，供运行时暂停并交给前端展示。
"""

from __future__ import annotations

import json
import re
from typing import Any

from backend.agent_runtime.answer_contract import (
    ANSWER_SCHEMA_VERSION,
    FACT_FORMATS,
    LIMITATION_CODES,
    RESULT_KINDS,
    TABLE_COLUMNS,
)

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_MAX_QUESTIONS = 8
_MAX_OPTIONS = 6
_MAX_STEPS = 8
_MAX_GOALS = 8
_PLAN_STATUSES = {"queued", "running", "complete", "skipped"}
_PLAN_KINDS = {"preparation", "clarification", "data", "finalize"}
_PLAN_COMPLETION_CRITERIA = {"tool_result", "user_answer", "validated_answer"}
_PLAN_DEFINITION_FIELDS = {"title", "description", "tool_names", "goal_ids", "kind", "completion_criteria"}
_SELECTION_MODES = {"single", "multiple"}
_MAX_SELECTIONS = 6


def _text(value: Any, field: str, maximum: int) -> str:
    result = str(value or "").strip()
    if not result:
        raise ValueError(f"{field} 不能为空")
    if len(result) > maximum:
        raise ValueError(f"{field} 不能超过 {maximum} 个字符")
    return result


def _id(value: Any, field: str) -> str:
    result = _text(value, field, 64)
    if not _ID.fullmatch(result):
        raise ValueError(f"{field} 只能包含字母、数字、下划线或短横线")
    return result


def normalize_options(raw_options: Any) -> list[dict[str, str]]:
    if raw_options is None:
        raw_options = []
    if not isinstance(raw_options, list):
        raise ValueError("options 必须是数组")
    if len(raw_options) > _MAX_OPTIONS:
        raise ValueError(f"单个问题最多提供 {_MAX_OPTIONS} 个选项")
    result: list[dict[str, str]] = []
    ids: set[str] = set()
    for index, raw in enumerate(raw_options):
        if not isinstance(raw, dict):
            raise ValueError(f"第 {index + 1} 个选项必须是对象")
        option_id = _id(raw.get("id"), f"第 {index + 1} 个选项 id")
        if option_id in ids:
            raise ValueError("选项 id 不能重复")
        ids.add(option_id)
        result.append(
            {
                "id": option_id,
                "label": _text(raw.get("label"), f"选项 {option_id} label", 120),
                "value": _text(raw.get("value"), f"选项 {option_id} value", 500),
            }
        )
    return result


def normalize_questions(raw_questions: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_questions, list):
        raise ValueError("questions 必须是数组")
    if not raw_questions:
        raise ValueError("至少需要提出一个问题")
    if len(raw_questions) > _MAX_QUESTIONS:
        raise ValueError(f"最多同时提出 {_MAX_QUESTIONS} 个问题")
    result: list[dict[str, Any]] = []
    ids: set[str] = set()
    for index, raw in enumerate(raw_questions):
        if not isinstance(raw, dict):
            raise ValueError(f"第 {index + 1} 个问题必须是对象")
        question_id = _id(raw.get("id"), f"第 {index + 1} 个问题 id")
        if question_id in ids:
            raise ValueError("问题 id 不能重复")
        ids.add(question_id)
        options = normalize_options(raw.get("options", []))
        allow_custom = raw.get("allow_custom", False)
        required = raw.get("required", True)
        selection_mode = raw.get("selection_mode", "single")
        max_selections = raw.get("max_selections")
        if selection_mode not in _SELECTION_MODES:
            raise ValueError(f"问题 {question_id} 的 selection_mode 必须是 single 或 multiple")
        if not isinstance(allow_custom, bool) or not isinstance(required, bool):
            raise ValueError(f"问题 {question_id} 的 allow_custom/required 必须是布尔值")
        if max_selections is None:
            max_selections = 1 if selection_mode == "single" else _MAX_SELECTIONS
        if (
            isinstance(max_selections, bool)
            or not isinstance(max_selections, int)
            or not 1 <= max_selections <= _MAX_SELECTIONS
        ):
            raise ValueError(f"问题 {question_id} 的 max_selections 必须是 1-{_MAX_SELECTIONS} 的整数")
        if selection_mode == "single" and max_selections != 1:
            raise ValueError(f"问题 {question_id} 为 single 时 max_selections 必须为 1")
        if not options and not allow_custom:
            raise ValueError(f"问题 {question_id} 必须有选项或允许自定义输入")
        result.append(
            {
                "id": question_id,
                "prompt": _text(raw.get("prompt"), f"问题 {question_id} prompt", 240),
                "options": options,
                "allow_custom": allow_custom,
                "required": required,
                "selection_mode": selection_mode,
                "max_selections": max_selections,
            }
        )
    return result


def normalize_question_text(value: Any) -> str:
    """Normalize visible question text for same-decision detection."""
    return re.sub(r"[\s\u3000]+", " ", str(value or "").strip().lower())


def question_fingerprint(question: dict[str, Any]) -> str:
    """Return a content fingerprint; model-generated question ids are not identity."""
    payload = {
        "prompt": normalize_question_text(question.get("prompt")),
        "options": sorted(
            normalize_question_text(option.get("value"))
            for option in question.get("options", [])
        ),
        "allow_custom": bool(question.get("allow_custom")),
        "required": bool(question.get("required", True)),
        "selection_mode": str(question.get("selection_mode") or "single"),
        "max_selections": int(question.get("max_selections") or 1),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def normalize_plan(raw_steps: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_steps, list):
        raise ValueError("steps 必须是数组")
    if not raw_steps:
        raise ValueError("计划至少需要一个步骤")
    if len(raw_steps) > _MAX_STEPS:
        raise ValueError(f"计划最多包含 {_MAX_STEPS} 个步骤")
    result: list[dict[str, Any]] = []
    ids: set[str] = set()
    for index, raw in enumerate(raw_steps):
        if not isinstance(raw, dict):
            raise ValueError(f"第 {index + 1} 个计划步骤必须是对象")
        step_id = _id(raw.get("id"), f"第 {index + 1} 个步骤 id")
        if step_id in ids:
            raise ValueError("步骤 id 不能重复")
        ids.add(step_id)
        raw_tools = raw.get("tool_names", [])
        if not isinstance(raw_tools, list) or len(raw_tools) > 12:
            raise ValueError(f"步骤 {step_id} 的 tool_names 必须是最多 12 项的数组")
        tool_names = [_text(name, f"步骤 {step_id} tool_name", 80) for name in raw_tools]
        raw_goal_ids = raw.get("goal_ids")
        if not isinstance(raw_goal_ids, list) or not 1 <= len(raw_goal_ids) <= _MAX_GOALS:
            raise ValueError(f"步骤 {step_id} 的 goal_ids 必须是 1 到 {_MAX_GOALS} 项数组")
        goal_ids = [_id(value, f"步骤 {step_id} goal_id") for value in raw_goal_ids]
        if len(goal_ids) != len(set(goal_ids)):
            raise ValueError(f"步骤 {step_id} 的 goal_ids 不能重复")
        kind = str(raw.get("kind") or "").strip()
        completion_criteria = str(raw.get("completion_criteria") or "").strip()
        # Keep legacy direct callers readable only when both fields are absent.
        # A partially specified pair is almost always a malformed plan and
        # must not silently override the field the model did provide.
        if not kind and not completion_criteria:
            if "finalize_answer" in tool_names:
                kind, completion_criteria = "finalize", "validated_answer"
            elif "ask_user" in tool_names:
                kind, completion_criteria = "clarification", "user_answer"
            elif tool_names:
                kind, completion_criteria = "data", "tool_result"
            else:
                kind, completion_criteria = "preparation", "tool_result"
        elif not kind or not completion_criteria:
            raise ValueError(f"步骤 {step_id} 必须同时提供 kind 和 completion_criteria")
        if kind not in _PLAN_KINDS:
            raise ValueError(f"步骤 {step_id} 的 kind 必须是 {_PLAN_KINDS}")
        if completion_criteria not in _PLAN_COMPLETION_CRITERIA:
            raise ValueError(f"步骤 {step_id} 的 completion_criteria 非法")
        expected_criteria = {
            "preparation": "tool_result",
            "clarification": "user_answer",
            "data": "tool_result",
            "finalize": "validated_answer",
        }[kind]
        if completion_criteria != expected_criteria:
            raise ValueError(f"步骤 {step_id} 的 kind 与 completion_criteria 不匹配")
        if kind == "finalize" and tool_names != ["finalize_answer"]:
            raise ValueError("finalize 步骤的 tool_names 必须只包含 finalize_answer")
        if kind == "clarification" and tool_names != ["ask_user"]:
            raise ValueError("clarification 步骤的 tool_names 必须只包含 ask_user")
        if kind in {"preparation", "data"} and not tool_names:
            raise ValueError(f"{kind} 步骤必须声明至少一个工具")
        if kind != "finalize" and "finalize_answer" in tool_names:
            raise ValueError("只有最后的 finalize 步骤可以调用 finalize_answer")
        result.append(
            {
                "id": step_id,
                "title": _text(raw.get("title"), f"步骤 {step_id} title", 120),
                "description": _text(raw.get("description"), f"步骤 {step_id} description", 300),
                "tool_names": tool_names,
                "goal_ids": goal_ids,
                "kind": kind,
                "completion_criteria": completion_criteria,
                "status": "queued",
            }
        )
    finalize_indexes = [index for index, step in enumerate(result) if step["kind"] == "finalize"]
    if finalize_indexes != [len(result) - 1]:
        raise ValueError("计划必须且只能有一个 finalize 步骤，并且它必须是最后一步")
    return result


def initialize_plan(raw_steps: Any) -> list[dict[str, Any]]:
    """Normalize an immutable plan and mark only its first step as runnable."""
    steps = normalize_plan(raw_steps)
    steps[0]["status"] = "running"
    return steps


def normalize_plan_updates(raw_updates: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_updates, list) or not raw_updates:
        raise ValueError("updates 必须是非空数组")
    if len(raw_updates) > _MAX_STEPS:
        raise ValueError(f"一次最多更新 {_MAX_STEPS} 个计划步骤")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_updates):
        if not isinstance(raw, dict):
            raise ValueError(f"第 {index + 1} 个计划更新必须是对象")
        step_id = _id(raw.get("id"), f"第 {index + 1} 个更新 id")
        status_value = raw.get("status")
        status = str(status_value or "").strip()
        if status and status not in _PLAN_STATUSES:
            raise ValueError(f"计划步骤 {step_id} 的 status 非法")
        reason = str(raw.get("reason") or "").strip()
        changes = {key: raw[key] for key in _PLAN_DEFINITION_FIELDS if key in raw}
        if not status and not changes:
            raise ValueError(f"计划步骤 {step_id} 必须包含状态更新或步骤修订")
        if (status == "skipped" or changes) and not reason:
            qualifier = "跳过" if status == "skipped" else "修订"
            raise ValueError(f"{qualifier}计划步骤 {step_id} 必须说明原因")
        if step_id in seen:
            raise ValueError("一次更新中步骤 id 不能重复")
        seen.add(step_id)
        update: dict[str, Any] = {"id": step_id, **changes}
        if status:
            update["status"] = status
        if reason:
            update["reason"] = _text(reason, f"计划步骤 {step_id} 修订原因", 300)
        if "title" in update:
            update["title"] = _text(update["title"], f"步骤 {step_id} title", 120)
        if "description" in update:
            update["description"] = _text(update["description"], f"步骤 {step_id} description", 300)
        if "tool_names" in update:
            raw_tools = update["tool_names"]
            if not isinstance(raw_tools, list) or len(raw_tools) > 12:
                raise ValueError(f"步骤 {step_id} 的 tool_names 必须是最多 12 项的数组")
            update["tool_names"] = [_text(name, f"步骤 {step_id} tool_name", 80) for name in raw_tools]
        if "goal_ids" in update:
            raw_goals = update["goal_ids"]
            if not isinstance(raw_goals, list) or not 1 <= len(raw_goals) <= _MAX_GOALS:
                raise ValueError(f"步骤 {step_id} 的 goal_ids 必须是 1 到 {_MAX_GOALS} 项数组")
            update["goal_ids"] = [_id(value, f"步骤 {step_id} goal_id") for value in raw_goals]
            if len(update["goal_ids"]) != len(set(update["goal_ids"])):
                raise ValueError(f"步骤 {step_id} 的 goal_ids 不能重复")
        result.append(update)
    return result


def apply_plan_updates(
    current_plan: list[dict[str, Any]], raw_updates: Any,
) -> list[dict[str, Any]]:
    """Apply linear transitions and bounded edits to queued future steps only."""
    updates = normalize_plan_updates(raw_updates)
    if not current_plan:
        raise ValueError("尚未设置计划，不能更新计划")
    plan = [dict(step) for step in current_plan]
    index_by_id = {str(step.get("id")): index for index, step in enumerate(plan)}
    for update in updates:
        if update["id"] not in index_by_id:
            raise ValueError(f"未知计划步骤 id：{update['id']}")
        index = index_by_id[update["id"]]
        current = str(plan[index].get("status") or "queued")
        target = str(update.get("status") or current)
        definition_changes = _PLAN_DEFINITION_FIELDS.intersection(update)
        if definition_changes:
            if current != "queued":
                raise ValueError(f"只能修订尚未开始的 queued 步骤：{update['id']}")
            for key in definition_changes:
                plan[index][key] = update[key]
            plan[index]["revision_reason"] = update.get("reason") or ""
        if current not in _PLAN_STATUSES:
            raise ValueError(f"计划步骤 {update['id']} 当前状态非法")
        if plan[index].get("kind") == "finalize" and target in {"complete", "skipped"}:
            raise ValueError("finalize 步骤只能由运行时在答案成功提交后完成")
        if current == target:
            continue
        if current == "complete" and target != "complete":
            raise ValueError(f"已完成的计划步骤 {update['id']} 不能回退")
        if current == "running" and target == "queued":
            raise ValueError(f"进行中的计划步骤 {update['id']} 不能回退")
        if current == "running" and target == "skipped":
            raise ValueError(f"进行中的计划步骤 {update['id']} 必须先完成或继续执行")
        if current == "queued" and target == "complete":
            raise ValueError(f"计划步骤 {update['id']} 必须先进入 running")
        if target == "running":
            if any(str(step.get("status")) == "running" for step in plan):
                raise ValueError("同一时间只能有一个 running 计划步骤")
            if any(str(step.get("status")) not in {"complete", "skipped"} for step in plan[:index]):
                raise ValueError(f"计划步骤 {update['id']} 的前置步骤尚未完成或跳过")
        plan[index]["status"] = target
        if target == "skipped":
            plan[index]["reason"] = update.get("reason") or ""
        elif target != "queued":
            plan[index].pop("reason", None)
    for step in plan:
        kind = str(step.get("kind") or "")
        criteria = str(step.get("completion_criteria") or "")
        expected = {
            "preparation": "tool_result", "clarification": "user_answer",
            "data": "tool_result", "finalize": "validated_answer",
        }.get(kind)
        if expected is None or criteria != expected:
            raise ValueError(f"计划步骤 {step.get('id')} 的 kind 与 completion_criteria 不匹配")
        tools = list(step.get("tool_names") or [])
        if kind == "finalize" and tools != ["finalize_answer"]:
            raise ValueError("finalize 步骤的 tool_names 必须只包含 finalize_answer")
        if kind == "clarification" and tools != ["ask_user"]:
            raise ValueError("clarification 步骤的 tool_names 必须只包含 ask_user")
        if kind in {"preparation", "data"} and not tools:
            raise ValueError(f"{kind} 步骤必须声明至少一个工具")
        if kind != "finalize" and "finalize_answer" in tools:
            raise ValueError("只有最后的 finalize 步骤可以调用 finalize_answer")
    finalize_indexes = [index for index, step in enumerate(plan) if step.get("kind") == "finalize"]
    if finalize_indexes != [len(plan) - 1]:
        raise ValueError("计划必须且只能有一个 finalize 步骤，并且它必须是最后一步")
    return plan


def validate_answers(
    questions: list[dict[str, Any]], answers: Any,
) -> list[dict[str, Any]]:
    if not isinstance(answers, list):
        raise ValueError("answers 必须是数组")
    question_map = {str(question["id"]): question for question in questions}
    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for raw in answers:
        if not isinstance(raw, dict):
            raise ValueError("每个回答必须是对象")
        question_id = _id(raw.get("question_id"), "question_id")
        if question_id not in question_map:
            raise ValueError(f"未知问题 id：{question_id}")
        if question_id in seen:
            raise ValueError(f"问题 {question_id} 不能重复回答")
        question = question_map[question_id]
        raw_values = raw.get("values")
        if raw_values is None:
            raw_value = raw.get("value")
            raw_values = [raw_value] if raw_value is not None else []
        if not isinstance(raw_values, list):
            raise ValueError(f"问题 {question_id} 的 values 必须是数组")
        values: list[str] = []
        for raw_value in raw_values:
            value = _text(raw_value, f"问题 {question_id} 的回答", 500)
            if value not in values:
                values.append(value)
        if not values and question.get("required", True):
            raise ValueError(f"问题 {question_id} 的回答不能为空")
        if len(values) > int(question.get("max_selections") or 1):
            raise ValueError(f"问题 {question_id} 最多选择 {question.get('max_selections')} 项")
        if question.get("selection_mode", "single") == "single" and len(values) > 1:
            raise ValueError(f"问题 {question_id} 只能选择一项")
        allowed_values = {str(item["value"]) for item in question.get("options", [])}
        if not question.get("allow_custom") and any(value not in allowed_values for value in values):
            raise ValueError(f"问题 {question_id} 只能选择提供的选项")
        seen.add(question_id)
        normalized.append({"question_id": question_id, "values": values})
    missing = [
        str(question["id"])
        for question in questions
        if question.get("required", True) and str(question["id"]) not in seen
    ]
    if missing:
        raise ValueError(f"缺少必答问题：{', '.join(missing)}")
    return normalized


def ask_user_handler(questions: Any) -> dict[str, Any]:
    return {"ok": True, "control": "ask_user", "questions": normalize_questions(questions)}


def declare_task_handler(goals: Any, requires_plan: bool = False) -> dict[str, Any]:
    from backend.agent_runtime.task_context import normalize_task_intent

    intent = normalize_task_intent(goals, requires_plan=requires_plan)
    return {
        "ok": True,
        "control": "declare_task",
        "goals": intent["goals"],
        "requires_plan": intent["needs_plan"],
    }


def set_plan_handler(steps: Any) -> dict[str, Any]:
    return {"ok": True, "control": "set_plan", "steps": normalize_plan(steps)}


def update_plan_handler(updates: Any) -> dict[str, Any]:
    return {"ok": True, "control": "update_plan", "updates": normalize_plan_updates(updates)}


def control_tool_specs() -> list[Any]:
    # Imported lazily so this module can also be used by state/validation tests
    # without creating a tool-registry import cycle during registry bootstrap.
    from backend.agent_runtime.tool_registry import ToolSpec

    common = {"type": "object", "additionalProperties": False}
    option_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "id": {"type": "string"},
            "label": {"type": "string"},
            "value": {"type": "string"},
        },
        "required": ["id", "label", "value"],
    }
    question_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "id": {"type": "string"},
            "prompt": {"type": "string"},
            "options": {"type": "array", "items": option_schema},
            "allow_custom": {"type": "boolean"},
            "required": {"type": "boolean"},
            "selection_mode": {"type": "string", "enum": ["single", "multiple"]},
            "max_selections": {"type": "integer", "minimum": 1, "maximum": _MAX_SELECTIONS},
        },
        "required": ["id", "prompt", "options", "allow_custom"],
    }
    goal_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "id": {"type": "string"},
            "description": {"type": "string", "maxLength": 300},
            "deliverable": {"type": "string", "maxLength": 200},
            "depends_on": {"type": "array", "maxItems": _MAX_GOALS, "items": {"type": "string"}},
        },
        "required": ["id", "description", "deliverable"],
    }
    step_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "id": {"type": "string"},
            "title": {"type": "string"},
            "description": {"type": "string"},
            "tool_names": {"type": "array", "items": {"type": "string"}},
            "goal_ids": {"type": "array", "minItems": 1, "maxItems": _MAX_GOALS, "items": {"type": "string"}},
            "kind": {"type": "string", "enum": sorted(_PLAN_KINDS)},
            "completion_criteria": {"type": "string", "enum": sorted(_PLAN_COMPLETION_CRITERIA)},
        },
        "required": ["id", "title", "description", "tool_names", "goal_ids", "kind", "completion_criteria"],
    }
    finalize_schema = {
        "type": "object", "additionalProperties": False,
        "properties": {
            "schema_version": {"type": "integer", "const": ANSWER_SCHEMA_VERSION},
            "result_kind": {"type": "string", "enum": sorted(RESULT_KINDS)},
            "goal_ids": {
                "type": "array", "minItems": 1, "maxItems": _MAX_GOALS, "uniqueItems": True,
                "items": {"type": "string", "pattern": r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"},
            },
            "blocks": {"type": "array", "minItems": 1, "maxItems": 16, "items": {
                "oneOf": [
                    {"type": "object", "additionalProperties": False,
                     "properties": {"kind": {"type": "string", "const": "text"}, "text": {"type": "string", "minLength": 1, "maxLength": 1000}},
                     "required": ["kind", "text"]},
                    {"type": "object", "additionalProperties": False,
                     "properties": {"kind": {"type": "string", "const": "fact_ref"}, "evidence_id": {"type": "string", "pattern": r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"},
                                    "format": {"type": "string", "enum": sorted(FACT_FORMATS)}},
                     "required": ["kind", "evidence_id", "format"]},
                    {"type": "object", "additionalProperties": False,
                     "properties": {"kind": {"type": "string", "const": "recommendation_table"},
                                    "entity_group_refs": {"type": "array", "minItems": 1, "maxItems": 32, "uniqueItems": True,
                                                           "items": {"type": "string", "pattern": r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"}},
                                    "columns": {"type": "array", "minItems": 1, "maxItems": len(TABLE_COLUMNS), "uniqueItems": True, "items": {"type": "string", "enum": sorted(TABLE_COLUMNS)}}},
                     "required": ["kind", "entity_group_refs", "columns"]},
                ]
            }},
            "limitation_codes": {"type": "array", "maxItems": 8, "uniqueItems": True, "items": {"type": "string", "enum": sorted(LIMITATION_CODES)}},
        }, "required": ["schema_version", "result_kind", "goal_ids", "blocks", "limitation_codes"],
    }

    def finalize_answer_handler(**draft: Any) -> dict[str, Any]:
        return {"ok": True, "control": "finalize_answer", "draft": draft}
    return [
        ToolSpec(
            "ask_user",
            "当缺少关键条件、数据工具报告用户明确提出的条件不支持，或存在多个会改变结果的合理口径时，向用户提出结构化问题并暂停当前运行。一般只在当前 running clarification 步骤调用；若当前 data 工具刚返回 unsupported_filter，运行时只允许针对该失败调用绑定的恢复提问，回答后 data 步骤仍保持 running，必须按新条件重试数据工具。问题必须是用户尚未决定的新决策；不能替用户选择，不能用普通文本假装提问，也不要用此工具审批 set_plan。互斥分支使用 single，可组合条件或多个品牌使用 multiple。收到回答前不要继续调用数据工具。",
            {**common, "properties": {"questions": {"type": "array", "items": question_schema}}, "required": ["questions"]},
            ask_user_handler,
            read_only=False, category="user_decision", effect="agent_control",
        ),
        ToolSpec(
            "declare_task",
            "每个新任务开始时先调用此工具。逐项列出用户要求的不同交付目标；不要把多个比较、计算或结论合并成一个目标。requires_plan 表示完成任务是否需要多个协调阶段，即使最终只有一个报告；多个独立目标或目标依赖会由运行时强制要求计划。",
            {
                **common,
                "properties": {
                    "goals": {"type": "array", "minItems": 1, "maxItems": _MAX_GOALS, "items": goal_schema},
                    "requires_plan": {"type": "boolean"},
                },
                "required": ["goals", "requires_plan"],
            },
            declare_task_handler,
            read_only=False,
            category="task_control",
            effect="agent_control",
        ),
        ToolSpec(
            "set_plan",
            "每个新任务都必须发布本次完整、简短、面向用户的计划，即使只有一个目标。最后一步必须是 kind=finalize、completion_criteria=validated_answer。为每步填写 goal_ids、tool_names、kind 和 completion_criteria。计划展示工作阶段，不是隐藏思维链。",
            {**common, "properties": {"steps": {"type": "array", "items": step_schema}}, "required": ["steps"]},
            set_plan_handler,
            read_only=False, category="plan_control", effect="agent_control",
        ),
        ToolSpec(
            "update_plan",
            "按顺序更新计划状态。可以有界修订尚未开始的 queued 步骤定义，但必须提供 reason，不能改写已完成或正在执行的步骤，也不能新增或删除步骤。clarification 在用户回答后由运行时收口；finalize 由运行时在答案成功验证并提交后收口。",
            {
                **common,
                "properties": {
                    "updates": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "id": {"type": "string"},
                                "status": {"type": "string", "enum": sorted(_PLAN_STATUSES)},
                                "reason": {"type": "string"},
                                "title": {"type": "string", "maxLength": 120},
                                "description": {"type": "string", "maxLength": 300},
                                "tool_names": {"type": "array", "maxItems": 12, "items": {"type": "string"}},
                                "goal_ids": {"type": "array", "minItems": 1, "maxItems": _MAX_GOALS, "items": {"type": "string"}},
                                "kind": {"type": "string", "enum": sorted(_PLAN_KINDS)},
                                "completion_criteria": {"type": "string", "enum": sorted(_PLAN_COMPLETION_CRITERIA)},
                            },
                            "required": ["id"],
                        },
                    }
                },
                "required": ["updates"],
            },
            update_plan_handler,
            read_only=False, category="plan_control", effect="agent_control",
        ),
        ToolSpec(
            "finalize_answer",
            "提交 schema_version=2 的结构化最终答案。用 fact_ref 和 recommendation_table 引用本次工具返回的证据，不要重复填写数值、单位、车型名或筛选条件。",
            finalize_schema,
            finalize_answer_handler,
            read_only=False,
            category="answer_control",
            effect="agent_control",
        ),
    ]
