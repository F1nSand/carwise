"""Hard preflight checks applied before registered or MCP tool handlers run."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from backend.agent_runtime.execution import ExecutionContext, check_execution
from backend.agent_runtime.tool_registry import ToolSpec, _validate_args, get_tool
from backend.agent_runtime.task_context import (
    normalize_task_intent,
    validate_plan_goal_coverage,
    validate_task_intent_upgrade,
)


def validate_finalize_answer(args: dict[str, Any], state: Any) -> Any:
    """Validate a proposed answer against evidence at the current task revision."""
    from backend.agent_runtime.answer_validation import validate_answer_draft
    from backend.data_loader import get_dataset_version

    revision = (state.task_context or {}).get("revision", 0)
    # Keep stale records in the validator input so a referenced old ID is
    # reported as stale_evidence, rather than disappearing and being called
    # unknown_evidence. The validator itself selects current records for use.
    evidence = [record for record in (state.evidence_records or []) if isinstance(record, dict)]
    required_goals = [
        str(goal.get("id")) for goal in (state.task_intent or {}).get("goals") or []
        if isinstance(goal, dict) and goal.get("id")
    ]
    validation = validate_answer_draft(
        args,
        evidence,
        dataset_version=get_dataset_version(),
        constraint_revision=revision,
        required_goal_ids=required_goals,
    )
    if not validation.valid:
        from backend.agent_runtime.answer_validation import record_answer_validation_failure
        record_answer_validation_failure(state, args, validation, phase="preflight")
    else:
        state.flags.pop("answer_no_progress", None)
    return validation


@dataclass(frozen=True)
class Allow:
    allowed: bool = True


@dataclass(frozen=True)
class Reject:
    error_code: str
    message: str
    repair_hint: str = ""
    allowed: bool = False


_FIELDS_BY_TOOL = {
    "price_compare": {"series", "brand", "year", "city"},
    "region_gap": {"series", "year", "budget_min_wan", "budget_max_wan", "brand", "city", "energy_type"},
    "price_decay": {"series", "brand", "city"},
    "recommend": {"budget_min_wan", "budget_max_wan", "city", "brand", "series", "energy_type"},
    "csv_query": {"series", "brand", "city", "year", "min_price_wan", "max_price_wan"},
    "price_distribution": {"budget_min_wan", "budget_max_wan", "brand", "city", "series", "year", "energy_type"},
    "mileage_price_correlation": {"budget_min_wan", "budget_max_wan", "brand", "city", "series", "year", "energy_type"},
    "model_comparison": {"budget_min_wan", "budget_max_wan", "brand", "city", "series", "year", "energy_type"},
    "brand_profile": {"budget_min_wan", "budget_max_wan", "brand", "city", "series", "year", "energy_type"},
    "age_price_relation": {"budget_min_wan", "budget_max_wan", "brand", "city", "series", "year", "energy_type"},
}
_SHARED_ANALYSIS_TOOLS = {
    "price_distribution", "mileage_price_correlation", "model_comparison", "brand_profile", "age_price_relation",
}
_SHARED_FILTER_CONSTRAINTS = {
    "budget_min_wan", "budget_max_wan", "brand", "brand_name", "brands", "city", "city_name", "cities",
    "series", "series_selectors", "year", "production_year", "year_min", "year_max", "energy_type",
    "energy_types", "price_min_yuan", "price_max_yuan", "mileage_min_km", "mileage_max_km",
    "age_min_years", "age_max_years",
}
for _shared_name in (*_SHARED_ANALYSIS_TOOLS, "region_gap"):
    _FIELDS_BY_TOOL[_shared_name] = _FIELDS_BY_TOOL.get(_shared_name, set()) | _SHARED_FILTER_CONSTRAINTS
_NON_DATA_CATEGORIES = {"user_decision", "task_control", "plan_control", "answer_control", "skill_control"}


def _is_data_tool(name: str, mcp_manager: Any | None = None) -> bool:
    # `load_skill` uses ToolSpec's default category for historical reasons,
    # but its result is instructions, never task evidence.
    if name in {"ask_user", "declare_task", "set_plan", "update_plan", "finalize_answer", "load_skill"}:
        return False
    if name in _FIELDS_BY_TOOL:
        return True
    if mcp_manager is not None and mcp_manager.has_model_tool(name):
        return True
    spec = get_tool(name)
    return bool(spec and spec.category == "domain_query" and spec.category not in _NON_DATA_CATEGORIES)


def _successful_step_result(state: Any, step: dict[str, Any], *, mcp_manager: Any | None = None) -> bool:
    step_id = str(step.get("id") or "")
    allowed_tools = {str(value) for value in step.get("tool_names") or []}
    goal_ids = {str(value) for value in step.get("goal_ids") or []}
    revision = int((state.task_context or {}).get("revision") or 0)
    for result in state.tool_results:
        name = str(result.get("tool_name") or "")
        output = result.get("output")
        if (
            result.get("status") != "done"
            or str(result.get("plan_step_id") or "") != step_id
            or name not in allowed_tools
            or int(result.get("constraint_revision", revision)) != revision
            or not isinstance(output, dict)
            or output.get("ok") is not True
        ):
            continue
        result_goals = {str(value) for value in result.get("goal_ids") or []}
        if not goal_ids.issubset(result_goals):
            continue
        criteria = str(step.get("completion_criteria") or "tool_result")
        if criteria == "user_answer":
            if name == "ask_user" and output.get("control") == "ask_user_response":
                return True
            continue
        if criteria == "tool_result":
            if step.get("kind") == "data":
                if _is_data_tool(name, mcp_manager):
                    return True
            elif name not in {"ask_user", "declare_task", "set_plan", "update_plan", "finalize_answer"}:
                return True
    return False


def _data_goal_ids(task_intent: dict[str, Any], plan: list[dict[str, Any]]) -> set[str]:
    return {
        str(goal_id)
        for step in plan
        if step.get("kind") == "data"
        for goal_id in (step.get("goal_ids") or [])
    }


def is_bound_unsupported_recovery(state: Any, step: dict[str, Any] | None) -> bool:
    """Whether a failed unsupported-filter result authorizes one bound question."""
    pending = state.flags.get("pending_user_decision")
    if (
        not isinstance(pending, dict)
        or pending.get("reason_code") != "unsupported_filter"
        or not isinstance(step, dict)
        or step.get("kind") != "data"
        or str(step.get("status") or "") != "running"
    ):
        return False
    call_id = str(pending.get("tool_call_id") or "")
    step_id = str(step.get("id") or "")
    goal_ids = {str(value) for value in step.get("goal_ids") or []}
    pending_goals = {str(value) for value in pending.get("goal_ids") or []}
    revision = int((state.task_context or {}).get("revision") or 0)
    if (
        not call_id
        or not step_id
        or str(pending.get("plan_step_id") or "") != step_id
        or not pending_goals
        or pending_goals != goal_ids
        or int(pending.get("constraint_revision", revision)) != revision
    ):
        return False
    result = next(
        (
            item for item in reversed(state.tool_results)
            if str(item.get("tool_call_id") or "") == call_id
        ),
        None,
    )
    output = result.get("output") if isinstance(result, dict) else None
    return bool(
        result
        and result.get("status") == "error"
        and str(result.get("plan_step_id") or "") == step_id
        and str(result.get("tool_name") or "") == str(pending.get("tool_name") or "")
        and {str(value) for value in result.get("goal_ids") or []} == pending_goals
        and isinstance(output, dict)
        and output.get("ok") is False
        and str(output.get("reason_code") or output.get("error_code") or "") == "unsupported_filter"
    )


def is_bound_ambiguous_series_recovery(state: Any, step: dict[str, Any] | None) -> bool:
    """Allow a clarification for a tool-bound catalog keyword error."""
    catalog_errors = {
        "ambiguous_series", "unknown_series", "ambiguous_brand", "unknown_brand",
        "ambiguous_city", "unknown_city",
    }
    pending = state.flags.get("pending_user_decision")
    if (
        not isinstance(pending, dict)
        or pending.get("reason_code") not in catalog_errors
        or not isinstance(step, dict)
        or step.get("kind") != "data"
        or str(step.get("status") or "") != "running"
    ):
        return False
    call_id = str(pending.get("tool_call_id") or "")
    step_id = str(step.get("id") or "")
    goal_ids = {str(value) for value in step.get("goal_ids") or []}
    pending_goals = {str(value) for value in pending.get("goal_ids") or []}
    revision = int((state.task_context or {}).get("revision") or 0)
    if (
        not call_id or not step_id
        or str(pending.get("plan_step_id") or "") != step_id
        or not pending_goals or pending_goals != goal_ids
        or int(pending.get("constraint_revision", revision)) != revision
    ):
        return False
    result = next((item for item in reversed(state.tool_results)
                   if str(item.get("tool_call_id") or "") == call_id), None)
    output = result.get("output") if isinstance(result, dict) else None
    return bool(
        result and result.get("status") == "error"
        and str(result.get("plan_step_id") or "") == step_id
        and str(result.get("tool_name") or "") == str(pending.get("tool_name") or "")
        and {str(value) for value in result.get("goal_ids") or []} == pending_goals
        and int(result.get("constraint_revision", revision)) == revision
        and isinstance(output, dict) and output.get("ok") is False
        and str(output.get("reason_code") or "") in catalog_errors
        and str(output.get("reason_code") or "") == str(pending.get("reason_code") or "")
        and (
            str(output.get("reason_code") or "").startswith("unknown_")
            or bool(output.get("candidate_values") or output.get("candidate_selectors"))
        )
    )
def _finite_and_bounded(value: Any, *, depth: int = 0) -> bool:
    if depth > 8:
        return False
    if isinstance(value, float) and not math.isfinite(value):
        return False
    if isinstance(value, str) and len(value) > 10_000:
        return False
    if isinstance(value, dict):
        return len(value) <= 100 and all(_finite_and_bounded(item, depth=depth + 1) for item in value.values())
    if isinstance(value, list):
        return len(value) <= 100 and all(_finite_and_bounded(item, depth=depth + 1) for item in value)
    return True


def preflight_tool_call(
    call: dict[str, Any], state: Any, *, execution_context: ExecutionContext | None = None,
    mcp_manager: Any | None = None,
) -> Allow | Reject:
    function = call.get("function") if isinstance(call, dict) else None
    function = function if isinstance(function, dict) else {}
    name = str(function.get("name") or "")
    call_id = str(call.get("id") or "").strip()
    if not call_id:
        return Reject("invalid_tool_call_id", "工具调用缺少有效 ID。")
    try:
        check_execution(execution_context)
    except Exception as exc:
        return Reject(str(getattr(exc, "code", "cancelled")), str(exc))
    if state.flags.get("pending_user_decision") and name != "ask_user":
        return Reject("decision_required", "需要先获得用户决定，当前调用不能执行。", "请只调用 ask_user。")
    args = function.get("arguments") or {}
    if not isinstance(args, dict):
        return Reject("invalid_arguments", "工具参数必须是 JSON 对象。")
    if not _finite_and_bounded(args):
        return Reject("invalid_arguments", "工具参数超过安全范围或包含非有限数值。")
    spec: ToolSpec | None = get_tool(name)
    if spec is None:
        if mcp_manager is None or not mcp_manager.has_model_tool(name):
            return Reject("unknown_tool", f"未注册的工具：{name}")
    else:
        error = _validate_args(spec, args)
        if error:
            return Reject("invalid_arguments", f"参数校验失败：{error}")
    task_intent = state.task_intent if isinstance(getattr(state, "task_intent", None), dict) else {}
    if name == "declare_task":
        try:
            candidate = normalize_task_intent(
                args.get("goals"),
                requires_plan=args.get("requires_plan", False),
                revision=int(task_intent.get("revision") or 0) + 1,
            )
            if task_intent:
                validate_task_intent_upgrade(task_intent, candidate)
                if not task_intent.get("legacy_synthetic_goal"):
                    limits = (state.run_budget or {}).get("limits") or {}
                    if state.flags.get("pending_plan_revision"):
                        raise ValueError("上一次目标修订尚未通过 update_plan 落入 queued 步骤")
                    if float((state.run_budget or {}).get("replans") or 0) >= float(limits.get("replans", 2)):
                        raise ValueError("任务修订已达到本次运行的重规划预算")
        except (TypeError, ValueError) as exc:
            return Reject(
                "invalid_task_intent" if not task_intent else "task_upgrade_rejected",
                str(exc),
                "首次声明要覆盖用户要求的每个独立交付目标；升级时保留旧目标并只添加新目标。",
            )
        return Allow()

    if name in {"set_plan", "update_plan", "finalize_answer"} and not task_intent:
        return Reject("task_intent_required", "请先调用 declare_task 提交本次任务的交付目标。", "先声明目标，再发布计划或提交答案。")
    if not task_intent and name != "declare_task":
        return Reject("task_intent_required", "每个新任务必须先调用 declare_task。", "先声明任务目标。")
    if task_intent and not state.plan and name not in {"declare_task", "set_plan"}:
        return Reject("plan_required", "计划尚未发布，当前工具不能执行。", "先调用 set_plan 发布本次任务的全程阶段。")

    if name == "set_plan":
        if state.plan:
            return Reject("plan_already_set", "本次任务的计划已经设置，不能覆盖。")
        try:
            validate_plan_goal_coverage(args.get("steps"), task_intent)
            from backend.agent_runtime.control_tools import normalize_plan

            normalized_plan = normalize_plan(args.get("steps"))
            for step in normalized_plan:
                if step["kind"] == "data" and not any(
                    _is_data_tool(tool_name, mcp_manager) for tool_name in step["tool_names"]
                ):
                    raise ValueError(f"data 步骤 {step['id']} 必须包含可执行的数据工具")
        except ValueError as exc:
            return Reject("plan_goal_coverage_invalid", str(exc), "修正步骤类型、完成条件与 goal_ids，确保最终步骤覆盖全部目标。")

    if name == "update_plan":
        if not state.plan:
            return Reject("plan_required", "当前没有可更新的计划。", "先调用 set_plan 发布覆盖全部目标的计划。")
        try:
            from backend.agent_runtime.control_tools import apply_plan_updates, normalize_plan_updates

            updates = normalize_plan_updates(args.get("updates"))
            proposed_plan = apply_plan_updates(state.plan, updates)
            validate_plan_goal_coverage(proposed_plan, task_intent)
            definitions_changed = any(
                any(
                    previous.get(field) != revised.get(field)
                    for field in ("title", "description", "tool_names", "goal_ids", "kind", "completion_criteria")
                )
                for previous, revised in zip(state.plan, proposed_plan)
            )
            for step in proposed_plan:
                if step.get("kind") == "data" and not any(
                    _is_data_tool(tool_name, mcp_manager) for tool_name in (step.get("tool_names") or [])
                ):
                    raise ValueError(f"data 步骤 {step.get('id')} 必须包含可执行的数据工具")
            if state.flags.get("pending_plan_revision") and not definitions_changed:
                raise ValueError("追加目标后必须先修订 queued 步骤以覆盖新目标")
            limits = (state.run_budget or {}).get("limits") or {}
            replans_used = float((state.run_budget or {}).get("replans") or 0)
            if definitions_changed and not state.flags.get("pending_plan_revision") and replans_used >= float(limits.get("replans", 2)):
                raise ValueError("计划修订已达到本次运行的重规划预算")
            for update in updates:
                if update.get("status") != "complete":
                    continue
                step = next((item for item in state.plan if str(item.get("id") or "") == update["id"]), None)
                if step is None or str(step.get("status") or "") != "running":
                    continue
                if step.get("kind") == "finalize":
                    raise ValueError("finalize 步骤只能由运行时收口")
                if not _successful_step_result(state, step, mcp_manager=mcp_manager):
                    raise ValueError(
                        f"计划步骤 {update['id']} 没有满足 {step.get('completion_criteria')} 的绑定成功结果"
                    )
        except ValueError as exc:
            code = "plan_step_result_required" if "没有满足" in str(exc) else "plan_revision_rejected"
            return Reject(code, str(exc), "仅在绑定结果成功后完成步骤；修订时只调整 queued 步骤并保留全部 goal_ids。")

    is_data_tool = _is_data_tool(name, mcp_manager)
    if name == "ask_user":
        if not task_intent:
            return Reject("task_intent_required", "提问前必须先声明任务目标并发布计划。", "先调用 declare_task 和 set_plan。")
        if not state.plan:
            return Reject("plan_required", "必须先发布本次任务计划，再向用户提问。", "调用 set_plan 并把提问放入 clarification 步骤。")
        active_step = next((item for item in state.plan if item.get("status") == "running"), None)
        planned_clarification = bool(
            active_step
            and active_step.get("kind") == "clarification"
            and name in set(active_step.get("tool_names") or [])
        )
        failed_data_recovery = (
            is_bound_unsupported_recovery(state, active_step)
            or is_bound_ambiguous_series_recovery(state, active_step)
        )
        if not planned_clarification and not failed_data_recovery:
            return Reject(
                "clarification_step_required",
                "ask_user 只能在计划中的 clarification 阶段调用；仅 unsupported_filter 或绑定当前 data 阶段的目录关键词错误可进行一次恢复提问。",
            )

    if is_data_tool:
        if not task_intent:
            return Reject("task_intent_required", "执行数据工具前必须先声明任务目标。", "先调用 declare_task。")
        if not state.plan:
            return Reject("plan_required", "所有新任务都必须先发布可展示的全程计划，才能执行数据工具。", "调用 set_plan，并为数据阶段填写 kind=data、completion_criteria=tool_result。")
        if state.flags.get("pending_plan_revision"):
            return Reject("plan_revision_required", "追加目标尚未纳入计划，不能继续执行数据工具。", "先用 update_plan 修订 queued 步骤并覆盖全部目标。")
        active_step = next((item for item in state.plan if item.get("status") == "running"), None)
        if not active_step:
            return Reject("plan_step_required", "当前没有 running 的计划步骤，不能执行数据工具。", "先按顺序更新计划，将下一数据步骤设为 running。")
        if active_step.get("kind") != "data":
            return Reject("plan_step_kind_mismatch", "数据工具只能在 kind=data 的 running 步骤中执行。")
        if name not in {str(value) for value in active_step.get("tool_names") or []}:
            return Reject("plan_tool_mismatch", f"工具 {name} 不属于当前计划步骤 {active_step.get('id')}。", "只执行当前步骤声明的工具。")

    if name == "load_skill" and state.plan:
        active_step = next((item for item in state.plan if item.get("status") == "running"), None)
        if active_step and name not in {str(value) for value in active_step.get("tool_names") or []}:
            return Reject("plan_tool_mismatch", f"工具 {name} 不属于当前计划步骤 {active_step.get('id')}。", "只执行当前步骤声明的工具。")

    constraints = state.task_context.get("constraints") or {}
    supported = _FIELDS_BY_TOOL.get(name, set())
    if name == "region_gap" and "filters" not in args:
        supported = {"series", "year"}
    for key, record in constraints.items():
        if key not in supported or not isinstance(record, dict):
            continue
        expected = record.get("value")
        if name in _SHARED_ANALYSIS_TOOLS or (name == "region_gap" and "filters" in args):
            filters = args.get("filters") if isinstance(args.get("filters"), dict) else {}
            if not _shared_filter_preserves_constraint(key, expected, filters):
                return Reject("constraint_changed", f"调用没有保留已确认条件 {key}。")
            continue
        actual = args.get(key)
        if key == "budget_max_wan":
            try:
                if actual is None or float(actual) > float(expected):
                    return Reject("constraint_changed", f"调用放宽了已确认预算上限 {expected}。")
            except (TypeError, ValueError):
                return Reject("constraint_changed", "调用没有保留已确认的预算上限。")
        elif key == "budget_min_wan":
            try:
                if actual is None or float(actual) < float(expected):
                    return Reject("constraint_changed", f"调用放宽了已确认预算下限 {expected}。")
            except (TypeError, ValueError):
                return Reject("constraint_changed", "调用没有保留已确认的预算下限。")
        elif str(actual or "").strip().casefold() != str(expected or "").strip().casefold():
            return Reject("constraint_changed", f"调用没有保留用户确认条件 {key}。")
    if name == "finalize_answer":
        goal_ids = [str(value) for value in args.get("goal_ids") or []]
        required_goal_ids = {str(goal.get("id")) for goal in task_intent.get("goals") or [] if isinstance(goal, dict)}
        if set(goal_ids) != required_goal_ids:
            return Reject("task_goal_incomplete", "最终答案没有明确覆盖所有已声明目标。", "goal_ids 必须逐项覆盖全部目标。")
        if not state.plan or state.flags.get("pending_plan_revision"):
            return Reject("plan_required", "最终交付前必须存在覆盖所有目标的当前计划。", "先发布计划或完成待处理的 queued 步骤修订。")
        final_step = state.plan[-1]
        active_step = next((item for item in state.plan if item.get("status") == "running"), None)
        if final_step.get("kind") != "finalize" or active_step is None or active_step.get("id") != final_step.get("id"):
            return Reject("finalize_step_required", "只有最后的 finalize 步骤处于 running 时才能提交答案。", "先收口前置步骤并启动最后的 finalize 步骤。")
        if any(str(step.get("status") or "") not in {"complete", "skipped"} for step in state.plan[:-1]):
            return Reject("plan_incomplete", "最终答案前的工作步骤尚未收口。", "先完成所有前置步骤，再启动 finalize。")
        data_steps = {
            str(step.get("id") or ""): step
            for step in state.plan
            if step.get("kind") == "data"
        }
        data_goals = _data_goal_ids(task_intent, state.plan)
        current_revision = int((state.task_context or {}).get("revision") or 0)
        covered_data_goals: set[str] = set()
        successful_calls: dict[str, set[str]] = {}
        for result in state.tool_results:
            step = data_steps.get(str(result.get("plan_step_id") or ""))
            tool_name = str(result.get("tool_name") or "")
            output = result.get("output")
            if (
                result.get("status") != "done"
                or step is None
                or tool_name not in {str(value) for value in step.get("tool_names") or []}
                or int(result.get("constraint_revision", current_revision)) != current_revision
                or not _is_data_tool(tool_name, mcp_manager)
                or not isinstance(output, dict)
                or output.get("ok") is not True
            ):
                continue
            result_goals = {str(value) for value in result.get("goal_ids") or []}
            covered_data_goals.update(result_goals & {str(value) for value in step.get("goal_ids") or []})
            successful_calls[str(result.get("tool_call_id") or "")] = result_goals & {str(value) for value in step.get("goal_ids") or []}
        try:
            from backend.data_loader import get_dataset_version
            current_dataset_version = get_dataset_version()
        except Exception:
            current_dataset_version = None
        evidence_goals = {
            str(goal_id)
            for record in (state.evidence_records or [])
            if isinstance(record, dict)
            and record.get("tool_call_id") in successful_calls
            and record.get("dataset_version") == current_dataset_version
            and int(record.get("constraint_revision", current_revision)) == current_revision
            for goal_id in set(map(str, record.get("goal_ids") or [])) & successful_calls[str(record.get("tool_call_id"))]
        }
        batch = state.pending_tool_batch or {}
        calls = list(batch.get("calls") or [])
        call_ids = [str(item.get("id") or "") for item in calls if isinstance(item, dict)]
        if call_id in call_ids and call_ids.index(call_id) != len(call_ids) - 1:
            return Reject("finalize_batch_not_closed", "finalize_answer 必须是当前工具批次的最后一个调用。", "将最终提交放在工具批次末尾。")
        validation = validate_finalize_answer(args, state)
        if not validation.valid:
            detail = "；".join(validation.errors)
            return Reject(
                "answer_validation_failed",
                detail or "答案没有通过证据校验。",
                "按所有结构化 validation issues 修正；区分 schema/protocol 问题和语义/证据问题，不要重复提交相同草稿。",
            )
        uncovered = data_goals - (covered_data_goals & evidence_goals)
        result_kind = str(args.get("result_kind") or "")
        if result_kind == "data_answer" and uncovered:
            # Preserve the more useful answer-validation diagnosis (especially
            # stale_evidence) above; this gate enforces tool-result binding.
            return Reject(
                "task_goal_without_data_result",
                f"部分数据目标缺少当前数据版本下绑定到成功结果的有效证据：{', '.join(sorted(uncovered))}。",
                "仅以当前条件和数据版本中绑定成功数据调用的 evidence 覆盖目标；控制工具不能替代证据。",
            )
        if result_kind == "no_data":
            empty_result_goals = {
                str(goal_id)
                for result in state.tool_results
                if result.get("status") == "done"
                and int(result.get("constraint_revision", current_revision)) == current_revision
                and result.get("dataset_version") == current_dataset_version
                and isinstance(result.get("output"), dict)
                and result["output"].get("ok") is True
                and (result["output"].get("result_status") == "empty" or result["output"].get("status") == "empty")
                for goal_id in (
                    set(map(str, result.get("goal_ids") or []))
                    & set(map(str, (data_steps.get(str(result.get("plan_step_id") or "")) or {}).get("goal_ids") or []))
                )
            }
            unsupported = data_goals - empty_result_goals
            if not data_goals or unsupported or (data_goals & evidence_goals):
                return Reject(
                    "no_data_without_empty_result",
                    "no_data 结果必须由当前条件、当前数据版本下绑定到每个数据目标的空结果调用支持。",
                    "仅在所有数据目标的计划工具都返回当前版本空结果时使用 no_data；有数据证据时提交 data_answer。",
                )
        elif result_kind == "explanation" and data_goals:
            return Reject(
                "data_result_kind_mismatch",
                "包含数据目标的任务不能用 explanation 替代数据结果。",
                "根据当前证据提交 data_answer、合法 no_data，或明确 partial_result。",
            )
    return Allow()


def _shared_filter_preserves_constraint(key: str, expected: Any, filters: dict[str, Any]) -> bool:
    """Compare confirmed legacy constraints with the shared filter units/shape."""
    if key in {"budget_min_wan", "budget_max_wan"}:
        bound = filters.get("price_min_yuan" if key == "budget_min_wan" else "price_max_yuan")
        if bound is None:
            return False
        try:
            expected_yuan = float(expected) * 10000
            return float(bound) >= expected_yuan if key == "budget_min_wan" else float(bound) <= expected_yuan
        except (TypeError, ValueError):
            return False
    numeric_bounds = {
        "price_min_yuan": ("price_min_yuan", "min"), "price_max_yuan": ("price_max_yuan", "max"),
        "mileage_min_km": ("mileage_min_km", "min"), "mileage_max_km": ("mileage_max_km", "max"),
        "age_min_years": ("age_min_years", "min"), "age_max_years": ("age_max_years", "max"),
        "year_min": ("year_min", "min"), "year_max": ("year_max", "max"),
    }
    if key in numeric_bounds:
        field, side = numeric_bounds[key]
        actual = filters.get(field)
        try:
            return actual is not None and (float(actual) >= float(expected) if side == "min" else float(actual) <= float(expected))
        except (TypeError, ValueError):
            return False
    target_map = {
        "brand": ("brands", True), "brand_name": ("brands", True), "brands": ("brands", True),
        "city": ("cities", True), "city_name": ("cities", True), "cities": ("cities", True),
        "series": ("series_selectors", True), "series_selectors": ("series_selectors", True),
        "year": ("year_min", "year_max"), "production_year": ("year_min", "year_max"),
        "energy_type": ("energy_types", True), "energy_types": ("energy_types", True),
    }
    target = target_map.get(key)
    if target is None:
        return True
    field = target[0]
    if field == "series_selectors":
        selected = filters.get(field)
        if not isinstance(selected, list) or not selected:
            return False
        if key == "series_selectors":
            expected_pairs = {(str(item.get("brand_name") or "").strip().casefold(), str(item.get("series") or "").strip().casefold())
                              for item in expected if isinstance(item, dict)} if isinstance(expected, list) else set()
            return bool(expected_pairs) and all(
                isinstance(item, dict) and (str(item.get("brand_name") or "").strip().casefold(),
                                            str(item.get("series") or "").strip().casefold()) in expected_pairs
                for item in selected)
        expected_values = {str(value).strip().casefold() for value in (expected if isinstance(expected, list) else [expected])}
        return all(str(item.get("series") or "").strip().casefold() in expected_values
                   for item in selected if isinstance(item, dict)) and all(isinstance(item, dict) for item in selected)
    if field in {"year_min", "year_max"}:
        try:
            year = int(expected)
            return filters.get("year_min") == year and filters.get("year_max") == year
        except (TypeError, ValueError):
            return False
    selected = filters.get(field)
    if field == "brands" and not selected:
        selectors = filters.get("series_selectors")
        selected = [item.get("brand_name") for item in selectors if isinstance(item, dict)] if isinstance(selectors, list) else []
    if not isinstance(selected, list) or not selected:
        return False
    expected_values = {str(value).strip().casefold() for value in (expected if isinstance(expected, list) else [expected])}
    return all(str(value).strip().casefold() in expected_values for value in selected)
