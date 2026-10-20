"""Serializable, source-aware user task state retained across compression/resume."""

from __future__ import annotations

import re
from typing import Any


_TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_MAX_TASK_GOALS = 8
_MAX_TASK_INTENT_UPGRADES = 2


def normalize_task_intent(
    raw_goals: Any,
    *,
    requires_plan: bool = False,
    revision: int = 1,
    source: str = "model_declared",
) -> dict[str, Any]:
    """Normalize requested deliverables and derive plan necessity server-side."""
    if not isinstance(raw_goals, list) or not 1 <= len(raw_goals) <= _MAX_TASK_GOALS:
        raise ValueError(f"goals 必须是 1 到 {_MAX_TASK_GOALS} 项数组")
    goals: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_goals):
        if not isinstance(raw, dict):
            raise ValueError(f"第 {index + 1} 个目标必须是对象")
        goal_id = str(raw.get("id") or "").strip()
        if not _TASK_ID.fullmatch(goal_id):
            raise ValueError(f"第 {index + 1} 个目标 id 格式无效")
        if goal_id in seen:
            raise ValueError("目标 id 不能重复")
        description = str(raw.get("description") or "").strip()
        deliverable = str(raw.get("deliverable") or "").strip()
        if not description or len(description) > 300:
            raise ValueError(f"目标 {goal_id} 的 description 必须为 1 到 300 个字符")
        if not deliverable or len(deliverable) > 200:
            raise ValueError(f"目标 {goal_id} 的 deliverable 必须为 1 到 200 个字符")
        raw_dependencies = raw.get("depends_on") or []
        if not isinstance(raw_dependencies, list) or len(raw_dependencies) > _MAX_TASK_GOALS:
            raise ValueError(f"目标 {goal_id} 的 depends_on 必须是数组")
        dependencies = [str(value).strip() for value in raw_dependencies]
        if len(dependencies) != len(set(dependencies)) or goal_id in dependencies:
            raise ValueError(f"目标 {goal_id} 的依赖不能重复或依赖自身")
        seen.add(goal_id)
        goals.append({
            "id": goal_id,
            "description": description,
            "deliverable": deliverable,
            "depends_on": dependencies,
        })
    all_ids = {goal["id"] for goal in goals}
    dependencies_by_goal: dict[str, set[str]] = {}
    for goal in goals:
        unknown = set(goal["depends_on"]) - all_ids
        if unknown:
            raise ValueError(f"目标 {goal['id']} 引用了未知依赖：{', '.join(sorted(unknown))}")
        dependencies_by_goal[goal["id"]] = set(goal["depends_on"])

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(goal_id: str) -> None:
        if goal_id in visiting:
            raise ValueError("任务目标依赖不能形成循环")
        if goal_id in visited:
            return
        visiting.add(goal_id)
        for dependency in dependencies_by_goal[goal_id]:
            visit(dependency)
        visiting.remove(goal_id)
        visited.add(goal_id)

    for goal_id in dependencies_by_goal:
        visit(goal_id)
    if not isinstance(requires_plan, bool):
        raise ValueError("requires_plan 必须是布尔值")
    # `requires_plan` is retained as an input compatibility field only. Every
    # new task has a visible work plan, including one-goal lookups.
    needs_plan = True
    return {
        "revision": max(1, int(revision)),
        "source": str(source),
        "mode": "planned",
        "complexity": "multi_goal" if len(goals) > 1 or any(goal["depends_on"] for goal in goals) else "single_goal",
        "needs_plan": needs_plan,
        "goals": goals,
    }


def validate_task_intent_upgrade(current: dict[str, Any], candidate: dict[str, Any]) -> None:
    """Allow bounded additive goal revisions without rewriting existing intent."""
    if current.get("legacy_synthetic_goal"):
        if not candidate.get("needs_plan"):
            raise ValueError("替换旧运行占位目标时必须保留全程计划")
        return
    if int(current.get("upgrades") or 0) >= _MAX_TASK_INTENT_UPGRADES:
        raise ValueError("当前任务声明已达到追加目标次数上限")
    old_goals = {str(goal.get("id")): goal for goal in current.get("goals") or [] if isinstance(goal, dict)}
    new_goals = {str(goal.get("id")): goal for goal in candidate.get("goals") or [] if isinstance(goal, dict)}
    if not old_goals or not set(old_goals).issubset(new_goals):
        raise ValueError("升级任务时必须保留已声明目标")
    for goal_id, old_goal in old_goals.items():
        if new_goals[goal_id] != old_goal:
            raise ValueError(f"升级任务时不能改写已有目标：{goal_id}")
    if len(new_goals) <= len(old_goals) or not candidate.get("needs_plan"):
        raise ValueError("任务修订必须只追加目标并保留计划")


def validate_plan_goal_coverage(steps: Any, task_intent: dict[str, Any]) -> None:
    if not isinstance(steps, list) or not steps:
        raise ValueError("计划至少需要一个步骤")
    goal_ids = {str(goal.get("id")) for goal in task_intent.get("goals") or [] if isinstance(goal, dict)}
    covered: set[str] = set()
    for step in steps:
        if not isinstance(step, dict):
            raise ValueError("计划步骤必须是对象")
        step_goals = step.get("goal_ids")
        if not isinstance(step_goals, list) or not step_goals:
            raise ValueError(f"计划步骤 {step.get('id') or '?'} 必须绑定至少一个目标")
        unknown = {str(value) for value in step_goals} - goal_ids
        if unknown:
            raise ValueError(f"计划步骤引用未知目标：{', '.join(sorted(unknown))}")
        covered.update(str(value) for value in step_goals)
    missing = goal_ids - covered
    if missing:
        raise ValueError(f"计划没有覆盖全部任务目标：{', '.join(sorted(missing))}")
    finalize_indexes = [
        index for index, step in enumerate(steps)
        if isinstance(step, dict) and str(step.get("kind") or "") == "finalize"
    ]
    if finalize_indexes != [len(steps) - 1]:
        raise ValueError("计划必须且只能有一个 finalize 步骤，并且它必须是最后一步")
    finalize_goals = {str(value) for value in steps[-1].get("goal_ids") or []}
    if finalize_goals != goal_ids:
        raise ValueError("最后的 finalize 步骤必须绑定全部任务目标")


def legacy_task_intent(user_text: str, plan: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Give pre-v3 checkpoints a stable compatibility goal without replaying tools."""
    old_plan = plan or []
    goal_ids: list[str] = []
    if old_plan:
        for step in old_plan:
            for value in step.get("goal_ids") or []:
                if str(value) not in goal_ids:
                    goal_ids.append(str(value))
    synthetic_goal = not goal_ids
    if synthetic_goal:
        goal_ids = ["legacy_task"]
    goals = [
        {
            "id": goal_id,
            "description": next(
                (str(step.get("description") or step.get("title") or "继续完成已保存计划") for step in old_plan if goal_id in (step.get("goal_ids") or [])),
                str(user_text or "继续完成已保存分析")[:300] or "继续完成已保存分析",
            ),
            "deliverable": "完成已保存的分析任务",
            "depends_on": [],
        }
        for goal_id in goal_ids[:_MAX_TASK_GOALS]
    ]
    intent = normalize_task_intent(goals, source="checkpoint_compat")
    # A checkpoint without a plan also has no durable task-goal contract. Let
    # its first resumed declare_task replace this placeholder exactly once;
    # treating it as an additive revision would strand every old simple run.
    if synthetic_goal and not old_plan:
        intent["legacy_synthetic_goal"] = True
    if old_plan:
        intent["needs_plan"] = True
        intent["mode"] = "planned"
        for step in old_plan:
            step.setdefault("goal_ids", list(goal_ids[:_MAX_TASK_GOALS]))
    return intent


def initial_task_context(goal: str, confirmed_filters: dict[str, Any] | None = None) -> dict[str, Any]:
    constraints = {
        str(key): {"value": value, "source": "confirmed_filters", "status": "confirmed"}
        for key, value in (confirmed_filters or {}).items()
        if value is not None and value != ""
    }
    return {
        "goal": str(goal or ""),
        "constraints": constraints,
        "decisions": [],
        "rejected_options": [],
        "evidence_refs": [],
        "blockers": [],
        "revision": 0,
    }


def apply_answer_records(context: dict[str, Any], answers: list[dict[str, Any]]) -> dict[str, Any]:
    """Apply only explicit structured answer forms; never infer from model prose."""
    result = dict(context or {})
    constraints = dict(result.get("constraints") or {})
    decisions = list(result.get("decisions") or [])
    changed = False
    for answer in answers:
        prompt = str(answer.get("prompt") or "")
        values = [str(value).strip() for value in answer.get("values") or [] if str(value).strip()]
        if not values and answer.get("value") is not None:
            values = [str(answer.get("value")).strip()]
        decision = {"question_id": answer.get("question_id"), "prompt": prompt,
                    "values": values, "source": answer.get("tool_call_id"), "status": "explicit"}
        decisions.append(decision)
        for value in values:
            match = re.fullmatch(r"([a-z][a-z0-9_]{1,63})\s*=\s*(.+)", value, re.IGNORECASE)
            if match:
                constraints[match.group(1)] = {"value": match.group(2).strip(), "source": answer.get("tool_call_id"), "status": "confirmed"}
                changed = True
            elif re.search(r"预算|budget", prompt, re.IGNORECASE):
                numeric = re.search(r"\d+(?:\.\d+)?", value)
                if numeric:
                    constraints["budget_max_wan"] = {"value": float(numeric.group()), "source": answer.get("tool_call_id"), "status": "confirmed"}
                    changed = True
    result.update({"constraints": constraints, "decisions": decisions})
    if changed:
        result["revision"] = int(result.get("revision") or 0) + 1
    return result


def context_summary(context: dict[str, Any]) -> str:
    """Produce a compact, replaceable prompt context from authoritative fields."""
    constraints = context.get("constraints") or {}
    rows = [f"- {key}: {value.get('value')} ({value.get('status')})" for key, value in constraints.items() if isinstance(value, dict)]
    decisions = context.get("decisions") or []
    rejected = context.get("rejected_options") or []
    blockers = context.get("blockers") or []
    parts = [f"当前目标：{context.get('goal') or '继续完成当前分析'}"]
    if rows:
        parts.append("已确认条件（按用户来源约束工具参数）：\n" + "\n".join(rows))
    if rejected:
        parts.append("用户已拒绝的选项：" + "；".join(map(str, rejected)))
    if decisions:
        parts.append("已回答问题不可重复询问。")
    if blockers:
        parts.append("当前阻塞条件：" + "；".join(map(str, blockers)) + "。必须先通过 ask_user 获取明确确认。")
    return "\n\n".join(parts)
