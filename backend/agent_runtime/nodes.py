"""单 Agent Runtime 节点：模型轮、工具轮、上下文统计和最终收口。"""

from __future__ import annotations

import copy
import hashlib
import json
from time import monotonic, perf_counter
import threading
from typing import Any
from uuid import uuid4

from backend.agent_runtime.state import AgentState
from backend.agent_runtime.message_protocol import MessageProtocolError, validate_message_protocol
from backend.agent_runtime.tool_contracts import normalize_tool_result
from backend.agent_runtime.execution import ExecutionContext, check_execution, ExecutionBoundaryError
from backend.agent_runtime.model_gateway import complete_turn, failure_code, ModelTurn, ModelTurnRejected
from backend.agent_runtime.run_budget import reserve, settle_model_usage
from backend.agent_runtime.answer_validation import record_answer_validation_failure
from backend.agent_runtime.recovery import tool_call_fingerprint, is_deterministic_failure
from backend.agent_runtime.tool_policy import (
    preflight_tool_call,
    validate_finalize_answer,
    is_bound_unsupported_recovery,
    is_bound_ambiguous_series_recovery,
    _is_data_tool,
    Reject,
)
from backend.agent_runtime.task_context import (
    apply_answer_records,
    normalize_task_intent,
    validate_plan_goal_coverage,
    validate_task_intent_upgrade,
)
from backend.agent_runtime.control_tools import (
    apply_plan_updates,
    initialize_plan,
    normalize_question_text,
    normalize_questions,
    normalize_plan,
    question_fingerprint,
    validate_answers,
)
from backend.agent_runtime.context_budget import (
    DEFAULT_CONTEXT_BUDGET,
    ContextBudget,
    ContextInputTooLarge,
    TokenEstimator,
    accumulate_usage,
    build_context_window,
)
from backend.agent_runtime.prefix import PrefixContext, build_prefix_context
from backend.agent_runtime.tool_registry import execute_registered_tool, stable_tool_schemas
from backend.agent_runtime.trace import RunTrace
from backend.config import DEEPSEEK_MODEL, MODEL_READ_TIMEOUT_S, is_thinking_model
from backend.prompt_utils import load_prompt


SYSTEM_PROMPT = load_prompt("agent_system.txt")


def _get(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def assistant_message_to_dict(message: Any) -> dict[str, Any]:
    """把 SDK assistant message 转成可再次发送的 OpenAI 消息字典。"""
    content = _get(message, "content", "") or ""
    output: dict[str, Any] = {"role": "assistant", "content": content}
    reasoning_content = _get(message, "reasoning_content")
    if reasoning_content is not None:
        output["reasoning_content"] = reasoning_content
    calls = []
    for call in _get(message, "tool_calls", []) or []:
        function = _get(call, "function", {}) or {}
        call_id = str(_get(call, "id", "") or "")
        calls.append(
            {
                "id": call_id,
                "type": str(_get(call, "type", "function")),
                "function": {
                    "name": str(_get(function, "name", "")),
                    "arguments": str(_get(function, "arguments", "{}")),
                },
            }
        )
    if calls:
        output["tool_calls"] = calls
    return output


def _trace_message(message: dict[str, Any]) -> dict[str, Any]:
    """移除隐藏思维链后再写入可观测轨迹，保留内部消息供模型续轮使用。"""
    output = copy.deepcopy(message)
    output.pop("reasoning_content", None)
    return output


def parse_tool_arguments(raw_arguments: str | dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    if isinstance(raw_arguments, dict):
        return raw_arguments, None
    if not isinstance(raw_arguments, str):
        return None, "工具参数必须是 JSON 对象"
    try:
        parsed = json.loads(raw_arguments)
    except json.JSONDecodeError as exc:
        return None, f"工具参数 JSON 无法解析：{exc.msg}"
    if not isinstance(parsed, dict):
        return None, "工具参数 JSON 必须是对象"
    return parsed, None


def _choice_message(response: Any) -> Any:
    choices = _get(response, "choices", []) or []
    if not choices:
        raise RuntimeError("模型响应缺少 choices")
    return _get(choices[0], "message", {})


def _usage(response: Any) -> dict[str, Any]:
    usage = _get(response, "usage")
    if usage is None:
        return {}
    if isinstance(usage, dict):
        return dict(usage)
    dump = getattr(usage, "model_dump", None)
    if callable(dump):
        value = dump(exclude_none=True)
        if isinstance(value, dict):
            return value
    attributes = getattr(usage, "__dict__", None)
    if isinstance(attributes, dict):
        return {str(key): value for key, value in attributes.items() if value is not None}
    return {
        key: getattr(usage, key)
        for key in ("prompt_tokens", "completion_tokens", "total_tokens", "prompt_cache_hit_tokens", "prompt_cache_miss_tokens")
        if getattr(usage, key, None) is not None
    }


def _record_model_usage(state: AgentState, response: Any, *, answer_text: str = "") -> dict[str, Any]:
    usage = _usage(response)
    state.flags["token_usage"] = accumulate_usage(state.flags.get("token_usage"), usage)
    settle_model_usage(
        state.run_budget,
        usage,
        estimated_prompt_tokens=int(state.flags.get("context_budget", {}).get("estimated_input_tokens") or 0),
        answer_text=answer_text,
    )
    for key in ("prompt_cache_hit_tokens", "prompt_cache_miss_tokens", "prompt_cache_hit"):
        if key in usage:
            state.flags[key] = usage[key]
    return usage


def _tool_call_fingerprint(tool_name: str, args: dict[str, Any], state: AgentState | None = None) -> str:
    from backend.data_loader import get_dataset_version
    try:
        version = get_dataset_version()
    except Exception:
        version = "unknown"
    return tool_call_fingerprint(
        tool_name, args or {}, dataset_version=version,
        constraint_revision=(state.task_context.get("revision", 0) if state else 0),
    )


def _unsupported_fields(output: dict[str, Any]) -> set[str]:
    observed = output.get("observed")
    if not isinstance(observed, dict):
        return set()
    fields: set[str] = set()
    field = observed.get("unsupported_field")
    if field:
        fields.add(str(field))
    raw_fields = observed.get("unsupported_fields")
    if isinstance(raw_fields, list):
        fields.update(str(item) for item in raw_fields if item)
    return fields


def _record_evidence(
    state: AgentState, tool_name: str, output: dict[str, Any], tool_call_id: str,
    *, dataset_version: str | None = None,
) -> None:
    if tool_name in {"ask_user", "declare_task", "set_plan", "update_plan", "finalize_answer", "load_skill", "read_report"}:
        return
    try:
        from backend.agent_runtime.evidence import evidence_from_tool_result
        records = evidence_from_tool_result(
            tool_name, output, tool_call_id,
            constraint_revision=state.task_context.get("revision", 0),
            dataset_version=dataset_version,
        )
        active_step = next((item for item in state.plan if item.get("status") == "running"), None)
        goal_ids = ([str(value) for value in active_step.get("goal_ids") or []] if active_step else
                    [str(goal.get("id")) for goal in (state.task_intent.get("goals") or []) if isinstance(goal, dict)])
        for record in records:
            record["goal_ids"] = goal_ids
            if active_step:
                record["plan_step_id"] = str(active_step.get("id") or "")
        existing = {str(item.get("evidence_id")) for item in state.evidence_records}
        state.evidence_records.extend(item for item in records if str(item.get("evidence_id")) not in existing)
        output["evidence"] = records[:32]
    except Exception:
        return


def _consume_answer_repair(state: AgentState, draft: Any, validation: Any, output: dict[str, Any], *, phase: str) -> None:
    """Count one rejected candidate once and retain its full diagnosis on exhaustion."""
    record = state.flags.get("answer_validation")
    fingerprint = str((record or {}).get("candidate_fingerprint") or "")
    if not isinstance(record, dict) or fingerprint != str(state.flags.get("answer_validation_candidate_fingerprint") or ""):
        record = record_answer_validation_failure(state, draft, validation, phase=phase)
        fingerprint = str(record.get("candidate_fingerprint") or "")
    previous_fingerprint = state.flags.get("answer_last_repair_fingerprint")
    unchanged = bool(state.flags.get("answer_no_progress") or (previous_fingerprint and previous_fingerprint == fingerprint))
    repairs = int(state.flags.get("answer_repair_count") or 0)
    limit = int((state.run_budget.get("limits") or {}).get("answer_repairs", 2))
    issues = record.get("issues") or []
    categories = {str(item.get("category") or "") for item in issues}
    if categories == {"protocol"}:
        counter = "answer_protocol_repairs"
    elif categories and categories <= {"schema"}:
        counter = "answer_schema_repairs"
    else:
        counter = "answer_semantic_repairs"
    remaining = max(0, limit - repairs)
    exhausted = unchanged or remaining <= 0 or not reserve(state.run_budget, "answer_repairs")
    state.flags[counter] = int(state.flags.get(counter) or 0) + 1
    if not exhausted:
        repairs += 1
        state.flags["answer_repair_count"] = repairs
        state.flags["answer_last_repair_fingerprint"] = fingerprint
        state.flags["answer_validation_attempts"] = repairs
        output["answer_validation"] = record
        budget_info = {"limit": limit, "used": repairs, "remaining": max(0, limit - repairs)}
        state.flags["answer_repair_budget"] = budget_info
        output["repair_budget"] = budget_info
        return
    state.flags["answer_validation_failed"] = True
    state.flags["answer_repair_exhausted"] = True
    state.flags["answer_repair_count"] = repairs
    state.flags["answer_validation_attempts"] = repairs + 1
    state.flags["answer_validation"] = record
    output["error_code"] = "answer_repair_exhausted"
    output["error"] = "答案校验修复已停止：草稿没有进展或修复预算已耗尽。"
    output["answer_validation"] = record
    output["attempts"] = repairs + 1
    output["candidate_fingerprint"] = fingerprint
    remaining_budget = max(0, limit - repairs)
    repair_budget = {"limit": limit, "used": repairs, "remaining": remaining_budget}
    state.flags["answer_repair_budget"] = repair_budget
    output["repair_budget"] = repair_budget
    output["repair_hint"] = "保留最后一次完整校验诊断；不能重复提交同一草稿。"
def _current_evidence_records(state: AgentState) -> list[dict[str, Any]]:
    revision = state.task_context.get("revision", 0)
    return [item for item in state.evidence_records if item.get("constraint_revision") == revision]


def _empty_recovery_scope(state: AgentState, step: dict[str, Any] | None = None) -> str:
    active = step or next((item for item in reversed(state.plan) if item.get("status") == "running"), {})
    payload = {
        "revision": int((state.task_context or {}).get("revision") or 0),
        "plan_step_id": str(active.get("id") or ""),
        "goal_ids": sorted(str(value) for value in active.get("goal_ids") or []),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _record_empty_recovery_result(
    state: AgentState, result_status: str, tool_name: str, mcp_manager: Any | None = None,
) -> None:
    """Count only actual empty data results, independently per step/goals/revision."""
    if not _is_data_tool(tool_name, mcp_manager):
        return
    active = next((item for item in reversed(state.plan) if item.get("status") == "running"), None)
    if not active or active.get("kind") != "data":
        return
    counts = dict(state.flags.get("empty_recovery_by_scope") or {})
    scope = _empty_recovery_scope(state, active)
    if result_status == "empty":
        counts[scope] = int(counts.get(scope) or 0) + 1
    elif result_status == "success":
        counts.pop(scope, None)
    state.flags["empty_recovery_by_scope"] = counts
    state.flags["empty_recovery_attempts"] = int(counts.get(scope) or 0)


def _empty_recovery_attempts_for_step(state: AgentState, step: dict[str, Any]) -> int:
    return int((state.flags.get("empty_recovery_by_scope") or {}).get(_empty_recovery_scope(state, step)) or 0)


def _track_tool_recovery(
    state: AgentState,
    tool_name: str,
    args: dict[str, Any],
    output: dict[str, Any],
    *,
    tool_call_id: str | None = None,
    plan_step_id: str | None = None,
    goal_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Remember unsupported/error calls so the same failed branch cannot loop forever."""
    if output.get("reason_code") == "unsupported_filter":
        fields = set(state.flags.get("unsupported_filter_fields") or [])
        fields.update(_unsupported_fields(output))
        state.flags["unsupported_filter_fields"] = sorted(fields)
        state.flags["pending_user_decision"] = {
            "fields": sorted(fields),
            "reason_code": "unsupported_filter",
            "tool_name": tool_name,
            "tool_call_id": str(tool_call_id or ""),
            "plan_step_id": str(plan_step_id or ""),
            "goal_ids": list(goal_ids or []),
            "constraint_revision": int((state.task_context or {}).get("revision") or 0),
        }
    elif (
        output.get("ok") is False
        and output.get("result_status") == "error"
        and output.get("reason_code") in {
            "ambiguous_series", "unknown_series", "ambiguous_brand", "unknown_brand",
            "ambiguous_city", "unknown_city",
        }
    ):
        state.flags["pending_user_decision"] = {
            "reason_code": str(output["reason_code"]),
            "tool_name": tool_name,
            "tool_call_id": str(tool_call_id or ""),
            "plan_step_id": str(plan_step_id or ""),
            "goal_ids": list(goal_ids or []),
            "constraint_revision": int((state.task_context or {}).get("revision") or 0),
            "keyword_field": str(output.get("keyword_field") or ""),
            "requested_keyword": str(output.get("requested_keyword") or output.get("requested_series") or ""),
            "candidate_values": copy.deepcopy(output.get("candidate_values") or []),
            "candidate_selectors": copy.deepcopy(output.get("candidate_selectors") or []),
        }

    if output.get("result_status") not in {"empty", "error"}:
        return output

    attempts = dict(state.flags.get("tool_call_attempts") or {})
    fingerprint = _tool_call_fingerprint(tool_name, args, state)
    count = int(attempts.get(fingerprint) or 0) + 1
    attempts[fingerprint] = count
    state.flags["tool_call_attempts"] = attempts
    if is_deterministic_failure(output):
        cache = dict(state.flags.get("tool_failure_cache") or {})
        cache[fingerprint] = output
        state.flags["tool_failure_cache"] = cache
    if count < 2:
        return output

    state.flags["repeated_tool_call_blocked"] = True
    return {
        **output,
        "recovery": {
            "status": "blocked_duplicate_call",
            "attempts": count,
            "fingerprint": fingerprint,
        },
        "note": f"已阻止重复调用 {tool_name} 的相同失败条件，请移除不支持或无效条件后继续。",
    }


def _answered_record(state: AgentState, question: dict[str, Any]) -> dict[str, Any] | None:
    fingerprint = question_fingerprint(question)
    for record in reversed(state.answered_inputs):
        source = str(record.get("source") or "user")
        if source == "user" and str(record.get("fingerprint") or "") == fingerprint:
            return record
    return None


def current_plan_step_id(state: AgentState) -> str | None:
    for step in state.plan:
        if str(step.get("status") or "") == "running":
            return str(step.get("id") or "") or None
    return None


def commit_tool_result(
    state: AgentState,
    tool_call_id: str,
    tool_name: str,
    args: dict[str, Any],
    output: dict[str, Any],
    status: str,
    duration_ms: int,
    *,
    batch_position: int | None = None,
    plan_step_id: str | None = None,
    goal_ids: list[str] | None = None,
    dataset_version: str | None = None,
    after_append: Any | None = None,
    checkpoint_sink: Any | None = None,
) -> None:
    """Close one call and advance its resumable batch at the same checkpoint boundary."""
    call_id = str(tool_call_id)
    if any(str(item.get("tool_call_id") or "") == call_id for item in state.tool_results):
        raise ValueError(f"工具调用 {call_id} 已经提交结果")
    state.add_tool_result(call_id, tool_name, args, output, status, duration_ms)
    state.tool_results[-1]["constraint_revision"] = int(state.task_context.get("revision") or 0)
    if dataset_version:
        state.tool_results[-1]["dataset_version"] = str(dataset_version)
    if plan_step_id:
        state.tool_results[-1]["plan_step_id"] = plan_step_id
    if goal_ids:
        state.tool_results[-1]["goal_ids"] = list(goal_ids)
    batch = state.pending_tool_batch
    if batch is not None:
        position = int(batch.get("next_index") or 0) if batch_position is None else int(batch_position)
        next_index = int(batch.get("next_index") or 0)
        calls = list(batch.get("calls") or [])
        if position != next_index or position >= len(calls):
            raise ValueError("工具结果与待恢复批次游标不一致")
        expected_id = str(calls[position].get("id") or "")
        if expected_id != call_id:
            raise ValueError("工具结果 ID 与待恢复批次调用不一致")
        completed = batch.setdefault("completed_call_ids", [])
        if call_id not in completed:
            completed.append(call_id)
        batch["next_index"] = position + 1
    if after_append is not None:
        after_append(state)
    if checkpoint_sink is not None:
        checkpoint_sink(state.to_checkpoint())


def plan_is_complete(state: AgentState) -> bool:
    return bool(state.plan) and all(str(step.get("status") or "") in {"complete", "skipped"} for step in state.plan)


def _close_answered_clarification(
    state: AgentState,
    *,
    tool_call_id: str,
    plan_step_id: str | None,
    goal_ids: list[str],
) -> dict[str, Any] | None:
    if not plan_step_id:
        return None
    step = next((item for item in state.plan if str(item.get("id") or "") == plan_step_id), None)
    if (
        step is None
        or step.get("kind") != "clarification"
        or step.get("completion_criteria") != "user_answer"
        or str(step.get("status") or "") != "running"
    ):
        return None
    result = next(
        (
            item for item in reversed(state.tool_results)
            if str(item.get("tool_call_id") or "") == tool_call_id
        ),
        None,
    )
    if (
        result is None
        or result.get("status") != "done"
        or str(result.get("tool_name") or "") != "ask_user"
        or not isinstance(result.get("output"), dict)
        or result["output"].get("control") != "ask_user_response"
    ):
        raise ValueError("澄清步骤缺少绑定原问题调用的有效用户回答")
    result_goals = {str(value) for value in result.get("goal_ids") or []}
    if not set(map(str, goal_ids)).issubset(result_goals):
        raise ValueError("澄清回答与原计划目标绑定不一致")
    updates = [{"id": plan_step_id, "status": "complete"}]
    next_step = next(
        (item for item in state.plan if item.get("status") == "queued"),
        None,
    )
    if next_step is not None:
        updates.append({"id": str(next_step.get("id") or ""), "status": "running"})
    state.plan = apply_plan_updates(state.plan, updates)
    revision = int(state.flags.get("plan_revision") or 0) + 1
    state.flags["plan_revision"] = revision
    return {"steps": state.plan, "revision": revision}


def _record_answered_inputs(
    state: AgentState,
    questions: list[dict[str, Any]],
    answers: list[dict[str, Any]],
    tool_call_id: str,
    source: str = "user",
    *,
    plan_step_id: str | None = None,
    goal_ids: list[str] | None = None,
    resume_revision: int | None = None,
    caused_by_tool_call_id: str | None = None,
) -> None:
    question_map = {str(question["id"]): question for question in questions}
    for answer in answers:
        question = question_map.get(str(answer.get("question_id") or ""))
        if not question:
            continue
        record = {
            "question_id": str(question["id"]),
            "prompt": str(question.get("prompt") or ""),
            "fingerprint": question_fingerprint(question),
            "values": [
                str(value)
                for value in (
                    answer.get("values")
                    or ([answer.get("value")] if answer.get("value") is not None else [])
                )
            ],
            "value": str(answer.get("value") or ((answer.get("values") or [""])[0] or "")),
            "tool_call_id": str(tool_call_id),
            "source": source,
            "plan_step_id": plan_step_id,
            "goal_ids": list(goal_ids or []),
            "resume_revision": resume_revision,
            "caused_by_tool_call_id": caused_by_tool_call_id,
        }
        existing = _answered_record(state, question)
        if existing is None:
            state.answered_inputs.append(record)
        else:
            existing.update(record)


def _log(state: AgentState, trace: RunTrace, event: dict[str, Any], step: int) -> None:
    summary = copy.deepcopy(event)
    event_input = summary.get("input")
    if isinstance(event_input, dict) and isinstance(event_input.get("messages"), list):
        messages = event_input["messages"]
        encoded = json.dumps(messages, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        event_input["messages"] = {
            "count": len(messages), "bytes": len(encoded),
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "roles": [str(item.get("role") or "") for item in messages if isinstance(item, dict)],
        }
    if isinstance(event_input, dict) and isinstance(event_input.get("tools"), list):
        tools = event_input["tools"]
        encoded = json.dumps(tools, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        event_input["tools"] = {"count": len(tools), "bytes": len(encoded),
                                 "sha256": hashlib.sha256(encoded).hexdigest()}
    output = summary.get("output")
    if isinstance(output, dict):
        content = output.get("content")
        if isinstance(content, str) and (len(content) > 512 or summary.get("event_type") == "model_round"):
            encoded = content.encode("utf-8")
            output["content"] = {"bytes": len(encoded), "sha256": hashlib.sha256(encoded).hexdigest()}
        if summary.get("event_type") == "model_round" and isinstance(output.get("tool_calls"), list):
            calls = output["tool_calls"]
            encoded = json.dumps(calls, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
            output["tool_calls"] = {"count": len(calls), "bytes": len(encoded),
                                    "sha256": hashlib.sha256(encoded).hexdigest()}
    state.run_logs.append({"step": step, **summary})
    trace.record(
        event.get("node", "agent"),
        event.get("event_type", "event"),
        summary.get("input", {}),
        summary.get("output", {}),
        summary.get("status", "ok"),
        int(summary.get("duration_ms", 0)),
        step=step,
        constraint_revision=int(state.task_context.get("revision") or 0),
    )


def route_node(state: AgentState, trace: RunTrace) -> None:
    started = perf_counter()
    if not state.user_text:
        state.flags["status"] = "invalid_input"
        state.flags["error"] = "用户问题不能为空"
        state.runtime_status = "error"
    else:
        state.flags["status"] = "ready"
        state.runtime_status = "running"
    _log(
        state,
        trace,
        {
            "node": "route",
            "event_type": "route",
            "input": {"user_text_length": len(state.user_text)},
            "output": dict(state.flags),
            "status": "ok",
            "duration_ms": int((perf_counter() - started) * 1000),
        },
        0,
    )


def agent_execute_node(
    state: AgentState,
    trace: RunTrace,
    client: Any,
    step: int,
    prefix: PrefixContext | None = None,
    context_budget: ContextBudget | None = None,
    token_estimator: TokenEstimator | None = None,
    execution_context: ExecutionContext | None = None,
    context_messages: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    started = perf_counter()
    context_prepared_ms = 0
    prefix_prepared_ms = 0
    model_latency_ms = 0
    turn: ModelTurn | None = None
    context_budget = context_budget or DEFAULT_CONTEXT_BUDGET
    token_estimator = token_estimator or TokenEstimator()
    response: Any = None
    recorded_responses: list[Any] = []
    current_run_start = state.flags.get("current_run_start")
    if not isinstance(current_run_start, int) or isinstance(current_run_start, bool):
        current_run_start = 0
    prefix_started = perf_counter()
    prefix = prefix or build_prefix_context(DEEPSEEK_MODEL, SYSTEM_PROMPT, stable_tool_schemas())
    prefix_prepared_ms = int((perf_counter() - prefix_started) * 1000)
    tools = copy.deepcopy(prefix.tools)
    thinking_mode = is_thinking_model(prefix.model)
    pending_user_decision = bool(state.flags.get("pending_user_decision"))
    if pending_user_decision and thinking_mode:
        tools = [tool for tool in tools if str((tool.get("function") or {}).get("name") or "") == "ask_user"]
    try:
        check_execution(execution_context)
        request_source_messages = [dict(message) for message in state.messages]
        extra_context = [dict(message) for message in (context_messages or [])]
        if extra_context:
            insertion = current_run_start
            if not 0 <= insertion < len(request_source_messages) or request_source_messages[insertion].get("role") != "user":
                insertion = next((index for index, message in enumerate(request_source_messages) if message.get("role") == "user"), len(request_source_messages))
            request_source_messages[insertion:insertion] = extra_context
            if insertion <= current_run_start:
                current_run_start += len(extra_context)
        validate_message_protocol(request_source_messages)
        context_window = build_context_window(
            request_source_messages,
            current_run_start=current_run_start,
            budget=context_budget,
            estimator=token_estimator,
            tools=tools,
        )
        context_prepared_ms = int((perf_counter() - started) * 1000)
    except ContextInputTooLarge as exc:
        state.flags["error_code"] = exc.code
        _log(
            state,
            trace,
            {
                "node": "agent_execute",
                "event_type": "model_error",
                "input": {"message_count": len(state.messages)},
                "output": {"error_code": exc.code},
                "status": "error",
                "duration_ms": int((perf_counter() - started) * 1000),
            },
            step,
        )
        return {"assistant": None, "error": exc.code, "error_code": exc.code, "usage": {}}
    request_messages = copy.deepcopy(context_window.messages)
    state.flags["context_budget"] = {
        "max_context_tokens": context_budget.max_context_tokens,
        "input_budget_tokens": context_budget.input_tokens,
        "estimated_input_tokens": context_window.estimated_input_tokens,
        "estimated_tool_schema_tokens": token_estimator.estimate_tools(tools),
        "estimator_source": "utf8_json_byte_approximation",
        "omitted_message_count": context_window.omitted_message_count,
        "compacted_tool_result_count": context_window.compacted_tool_result_count,
        "request_max_tokens": context_budget.reserved_output_tokens,
    }
    tool_choice = (
        "auto"
        if thinking_mode
        else (
            {"type": "function", "function": {"name": "ask_user"}}
            if pending_user_decision
            else "auto"
        )
    )
    state.flags["prefix_hash"] = prefix.prefix_hash
    state.flags["prefix_cache_hit"] = prefix.cache_hit
    request = {
        "model": prefix.model,
        "messages": request_messages,
        "tools": tools,
        "tool_choice": tool_choice,
        "stream": False,
        "temperature": 0.1,
        "max_tokens": context_budget.reserved_output_tokens,
    }

    def complete_with_deadline(call_request: dict[str, Any]) -> ModelTurn:
        check_execution(execution_context)
        if token_estimator.estimate_request(call_request["messages"], call_request.get("tools") or []) > context_budget.input_tokens:
            raise ModelTurnRejected("context_input_too_large", "补救提示会超过模型输入预算，未发起请求。")
        bounded = dict(call_request)
        if execution_context is not None:
            bounded["timeout"] = min(MODEL_READ_TIMEOUT_S, execution_context.remaining_seconds())
        return complete_turn(client, **bounded)

    def record_response_usage(candidate: Any, answer_text: str = "") -> dict[str, Any]:
        if candidate is None or any(candidate is recorded for recorded in recorded_responses):
            return {}
        # Keep references for this bounded node execution. Comparing identity
        # alone after releasing an SDK response can miss a later object if its
        # Python id is reused.
        recorded_responses.append(candidate)
        if not answer_text:
            try:
                answer_text = str(_get(_choice_message(candidate), "content", "") or "")
            except Exception:
                pass
        return _record_model_usage(state, candidate, answer_text=answer_text)

    def recover_truncated_response(retry_request: dict[str, Any]) -> None:
        nonlocal turn, response, model_latency_ms
        if turn is None or turn.category != "truncated":
            return
        partial = _choice_message(response)
        partial_text = str(_get(partial, "content", "") or "")
        first_usage = record_response_usage(response, partial_text)
        _log(
            state,
            trace,
            {
                "node": "agent_execute",
                "event_type": "model_truncation_retry",
                "input": {"model": prefix.model, "max_tokens": request["max_tokens"]},
                "output": {
                    "finish_reason": turn.finish_reason,
                    "provider_request_id": turn.provider_request_id,
                    "usage": first_usage,
                },
                "status": "retrying",
                "duration_ms": turn.latency_ms,
            },
            step,
        )
        if not reserve(state.run_budget, "truncation_retries") or not reserve(state.run_budget, "model_calls"):
            raise ModelTurnRejected("model_truncated", "模型响应因长度限制被截断，且没有剩余预算重新生成。")
        retry_messages = [
            *retry_request["messages"],
            {
                "role": "system",
                "content": "上一次完整响应达到输出长度上限。请缩短说明，只返回完整、可解析的工具调用或完整答案；不要复述中间分析。",
            },
        ]
        if token_estimator.estimate_request(retry_messages, tools) > context_budget.input_tokens:
            raise ModelTurnRejected(
                "model_truncated",
                "模型响应已截断；添加修复提示会超过输入预算，因此没有发起第二次请求。",
            )
        turn = complete_with_deadline({**retry_request, "messages": retry_messages})
        model_latency_ms += turn.latency_ms
        response = turn.response
        record_response_usage(response, str(_get(partial, "content", "") or ""))
        check_execution(execution_context)
        if turn.category == "truncated":
            partial = _choice_message(response)
            raise ModelTurnRejected("model_truncated", "模型重新生成后仍因长度限制被截断。")

    try:
        turn = complete_with_deadline(request)
        model_latency_ms += turn.latency_ms
        response = turn.response
        record_response_usage(response)
        check_execution(execution_context)
        if turn.category == "truncated":
            recover_truncated_response(request)
            if turn is not None and turn.category == "truncated":
                raise ModelTurnRejected("model_truncated", "模型响应因长度限制被截断。")
        if turn.category == "empty":
            raise ModelTurnRejected("model_empty_response", "模型返回了空响应。")
        assistant = assistant_message_to_dict(turn.message)
        try:
            candidate = [*state.messages, assistant]
            validate_message_protocol(candidate, allow_pending_tail=True)
        except MessageProtocolError as protocol_error:
            # Invalid provider output is retained only in the internal trace. It
            # never enters the native message chain or creates fabricated IDs.
            invalid = _trace_message(assistant)
            _log(
                state, trace,
                {"node": "agent_execute", "event_type": "tool_protocol_rejected",
                 "input": {"model": prefix.model},
                 "output": {"error": str(protocol_error), "assistant": invalid},
                 "status": "rejected", "duration_ms": 0},
                step,
            )
            record_response_usage(response, str(assistant.get("content") or ""))
            if not reserve(state.run_budget, "model_calls"):
                raise RuntimeError("修复工具调用协议时已达到模型调用预算")
            correction_request = {**request, "messages": [
                *request_messages,
                {"role": "system", "content": "上一次工具调用违反协议（ID 必须非空且本批次唯一）。请重新决策并返回有效调用；不要复用无效调用。"},
            ]}
            turn = complete_with_deadline(correction_request)
            model_latency_ms += turn.latency_ms
            response = turn.response
            record_response_usage(response)
            check_execution(execution_context)
            if turn.category == "truncated":
                recover_truncated_response(correction_request)
                if turn is not None and turn.category == "truncated":
                    raise ModelTurnRejected("model_truncated", "模型修复响应因长度限制被截断。")
            if turn.category == "empty":
                raise ModelTurnRejected("model_empty_response", "模型修复响应为空。")
            assistant = assistant_message_to_dict(turn.message)
            validate_message_protocol([*state.messages, assistant], allow_pending_tail=True)
    except Exception as exc:
        record_response_usage(response)
        duration_ms = int((perf_counter() - started) * 1000)
        code = str(getattr(exc, "code", "") or failure_code(exc))
        state.flags["failure_code"] = code
        turn_details = {
            "error_code": code,
            "response_category": turn.category if turn is not None else None,
            "finish_reason": turn.finish_reason if turn is not None else None,
            "provider_request_id": turn.provider_request_id if turn is not None else None,
            "request_max_tokens": request["max_tokens"],
            "estimated_input_tokens": int(state.flags.get("context_budget", {}).get("estimated_input_tokens") or 0),
            "usage": _usage(response) if response is not None else {},
            "model_latency_ms": model_latency_ms,
            "context_prepare_ms": context_prepared_ms,
            "prefix_prepare_ms": prefix_prepared_ms,
            "total_duration_ms": duration_ms,
        }
        _log(
            state,
            trace,
            {
                "node": "agent_execute",
                "event_type": "model_response_rejected" if isinstance(exc, ModelTurnRejected) else "model_error",
                "input": {"model": prefix.model, "message_count": len(request_messages)},
                "output": {"error": str(exc), **turn_details},
                "status": "error",
                "duration_ms": duration_ms,
            },
            step,
        )
        return {"assistant": None, "error": str(exc), "error_code": code, "usage": {}}

    state.append_message(assistant)
    duration_ms = int((perf_counter() - started) * 1000)
    usage = _usage(response)
    record_response_usage(response, str(assistant.get("content") or ""))
    _log(
        state,
        trace,
        {
            "node": "agent_execute",
            "event_type": "model_round",
            "input": {
                **request,
                "messages": [_trace_message(message) for message in request_messages],
                "context_budget": dict(state.flags["context_budget"]),
            },
            "output": _trace_message(assistant),
            "response_category": turn.category if turn is not None else "unknown",
            "finish_reason": turn.finish_reason if turn is not None else None,
            "provider_request_id": turn.provider_request_id if turn is not None else None,
            "model_latency_ms": model_latency_ms,
            "context_prepare_ms": context_prepared_ms,
            "prefix_prepare_ms": prefix_prepared_ms,
            "post_response_ms": max(0, duration_ms - context_prepared_ms - prefix_prepared_ms - model_latency_ms),
            "status": "ok",
            "duration_ms": duration_ms,
        },
        step,
    )
    return {"assistant": assistant, "error": None, "error_code": None, "usage": usage}


def tool_execute_node(
    state: AgentState,
    trace: RunTrace,
    tool_calls: list[dict[str, Any]],
    step: int,
    mcp_manager: Any | None = None,
    event_emitter: Any | None = None,
    run_id: str | None = None,
    cancellation: threading.Event | None = None,
    execution_context: ExecutionContext | None = None,
    checkpoint_sink: Any | None = None,
) -> dict[str, Any] | None:
    """执行一组模型工具调用；返回 waiting_user 控制信号时立即暂停。"""
    batch = state.pending_tool_batch
    start_index = int(batch.get("next_index") or 0) if batch else 0
    for position in range(start_index, len(tool_calls)):
        try:
            check_execution(execution_context)
        except ExecutionBoundaryError as exc:
            return {"status": exc.code, "error": str(exc)}
        tool_call = tool_calls[position]
        started = perf_counter()
        function = tool_call.get("function") or {}
        tool_call_id = str(tool_call.get("id") or "")
        tool_name = str(function.get("name") or "")
        tool_dataset_version: str | None = None
        raw_arguments = function.get("arguments", "{}")
        active_step = next((item for item in state.plan if item.get("status") == "running"), None)
        plan_step_id = str(active_step.get("id") or "") if active_step else None
        goal_ids = (
            [str(value) for value in active_step.get("goal_ids") or []]
            if active_step
            else [str(goal.get("id")) for goal in (state.task_intent.get("goals") or []) if isinstance(goal, dict)]
        )
        used_tool_call_ids = {
            str(item.get("tool_call_id") or "")
            for item in state.tool_results
            if item.get("tool_call_id")
        }
        duplicate_tool_call_id = tool_call_id in used_tool_call_ids or (
            state.pending_input is not None
            and str(state.pending_input.get("tool_call_id") or "") == tool_call_id
        )
        start_event = {
            "node": "tool_execute",
            "event_type": "tool_started",
            "input": {
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
                "arguments": raw_arguments,
                "plan_step_id": plan_step_id,
                "goal_ids": goal_ids,
            },
            "output": {},
            "status": "running",
            "duration_ms": 0,
        }
        args, parse_error = parse_tool_arguments(raw_arguments)
        preflight_rejected = False

        if duplicate_tool_call_id:
            output = {"ok": False, "tool": tool_name, "error": f"tool_call_id 重复：{tool_call_id}"}
            status = "error"
            safe_args = {}
        elif parse_error:
            preflight_rejected = True
            output = {"ok": False, "tool": tool_name, "error": parse_error}
            status = "error"
            safe_args: dict[str, Any] = {}
        else:
            safe_args = args or {}
            # The preflight boundary receives normalized arguments. OpenAI
            # represents function.arguments as JSON text; rejecting that raw
            # string here would incorrectly reject every valid call.
            preflight_call = {
                **tool_call,
                "function": {**(tool_call.get("function") or {}), "arguments": safe_args},
            }
            decision = preflight_tool_call(
                preflight_call, state, execution_context=execution_context, mcp_manager=mcp_manager
            )
            fingerprint = _tool_call_fingerprint(tool_name, safe_args, state)
            cached_failure = (state.flags.get("tool_failure_cache") or {}).get(fingerprint)
            if isinstance(decision, Reject):
                preflight_rejected = True
                output = {"ok": False, "tool": tool_name, "error_code": decision.error_code,
                          "error": decision.message, "repair_hint": decision.repair_hint}
                status = "error"
                if tool_name == "finalize_answer" and decision.error_code == "answer_validation_failed":
                    output["answer_validation"] = state.flags.get("answer_validation")
                    _consume_answer_repair(state, safe_args, None, output, phase="preflight")
            elif cached_failure:
                preflight_rejected = True
                output = {**cached_failure, "ok": False,
                          "recovery": {"status": "blocked_duplicate_call", "fingerprint": fingerprint}}
                status = "error"
            elif not reserve(state.run_budget, "tool_executions"):
                preflight_rejected = True
                output = {"ok": False, "tool": tool_name, "error_code": "tool_budget_exhausted",
                          "error": "已达到整个分析任务的工具执行预算。"}
                status = "error"
            else:
                _log(state, trace, start_event, step)
                if event_emitter is not None:
                    event_emitter()
                try:
                    if tool_name in {"price_compare", "region_gap", "price_decay", "recommend", "csv_query",
                                     "price_distribution", "mileage_price_correlation", "model_comparison",
                                     "brand_profile", "age_price_relation"}:
                        # Bind even an empty result to the exact local dataset
                        # identity observed immediately before the handler runs.
                        from backend.data_loader import get_dataset_version
                        tool_dataset_version = get_dataset_version()
                    if tool_name == "declare_task":
                        # Keep task-state transitions inside the runtime control
                        # plane; data-tool adapters and MCP providers cannot
                        # alter or drop the declared task contract.
                        from backend.agent_runtime.control_tools import declare_task_handler

                        output = declare_task_handler(
                            safe_args.get("goals"),
                            requires_plan=safe_args.get("requires_plan", False),
                        )
                    elif mcp_manager is not None and mcp_manager.has_model_tool(tool_name):
                        from backend.agent_runtime.mcp import MCPInvocationContext

                        output = mcp_manager.call_model_tool(
                            tool_name,
                            safe_args,
                            context=MCPInvocationContext(
                                call_id=tool_call_id,
                                idempotency_key=mcp_manager.idempotency_key(run_id or "", tool_call_id),
                                deadline_monotonic=min(
                                    execution_context.deadline_monotonic if execution_context else float("inf"),
                                    monotonic() + mcp_manager.timeout_for(tool_name),
                                ),
                                cancellation=cancellation or threading.Event(),
                                run_id=str(run_id or ""),
                                attempt=1,
                            ),
                        )
                    else:
                        output = execute_registered_tool(tool_name, safe_args)
                    if not isinstance(output, dict):
                        output = {"ok": False, "tool": tool_name, "error": "工具必须返回 JSON 对象"}
                except Exception as exc:
                    output = {"ok": False, "tool": tool_name, "error": f"工具执行失败：{exc}"}
                status = "done" if output.get("ok") else "error"

        try:
            check_execution(execution_context)
        except ExecutionBoundaryError as exc:
            output = {"ok": False, "tool": tool_name, "error_code": exc.code, "error": str(exc)}
            status = "error"

        output = normalize_tool_result(tool_name, output)
        status = "done" if output.get("ok") else "error"

        output = _track_tool_recovery(
            state,
            tool_name,
            safe_args,
            output,
            tool_call_id=tool_call_id,
            plan_step_id=plan_step_id,
            goal_ids=goal_ids,
        )
        if output.get("result_status") in {"empty", "success"}:
            _record_empty_recovery_result(state, str(output.get("result_status")), tool_name, mcp_manager)

        if status == "done" and tool_name == "declare_task":
            try:
                previous = state.task_intent
                revision = int(previous.get("revision") or 0) + 1 if previous else 1
                updated = normalize_task_intent(
                    output.get("goals"),
                    requires_plan=bool(output.get("requires_plan", False)),
                    revision=revision,
                )
                if previous:
                    validate_task_intent_upgrade(previous, updated)
                    if previous.get("legacy_synthetic_goal"):
                        # This replaces a migration-only placeholder for a
                        # checkpoint that never had a durable task contract.
                        # It is initialization, not a user-requested replan.
                        state.flags.pop("pending_plan_revision", None)
                        state.flags["legacy_task_intent_replaced"] = True
                    else:
                        if not reserve(state.run_budget, "replans"):
                            raise ValueError("任务修订已达到本次运行的重规划预算")
                        updated["upgrades"] = int(previous.get("upgrades") or 0) + 1
                        state.flags["task_intent_upgrades"] = updated["upgrades"]
                        state.flags["pending_plan_revision"] = True
                state.task_intent = updated
                _log(
                    state,
                    trace,
                    {
                        "node": "control",
                        "event_type": "task_intent_update",
                        "input": {"tool_call_id": tool_call_id},
                        "output": {"task_intent": updated},
                        "status": "ok",
                        "duration_ms": 0,
                    },
                    step,
                )
            except ValueError as exc:
                output = {"ok": False, "tool": tool_name, "error_code": "task_upgrade_rejected", "error": str(exc)}
                status = "error"

        if status == "done" and tool_name == "set_plan":
            try:
                if state.plan:
                    raise ValueError("计划已经设置，不能再次调用 set_plan")
                validate_plan_goal_coverage(output.get("steps"), state.task_intent)
                state.plan = initialize_plan(output.get("steps"))
                state.flags["plan_revision"] = int(state.flags.get("plan_revision") or 0) + 1
                state.runtime_status = "running"
                state.flags["runtime_status"] = state.runtime_status
                _log(
                    state,
                    trace,
                    {
                        "node": "control",
                        "event_type": "plan_update",
                        "input": {"tool_call_id": tool_call_id, "plan_step_id": plan_step_id},
                        "output": {"steps": state.plan, "revision": int(state.flags.get("plan_revision") or 0)},
                        "status": "ok",
                        "duration_ms": 0,
                    },
                    step,
                )
            except ValueError as exc:
                output = {"ok": False, "tool": tool_name, "error": str(exc)}
                status = "error"

        if status == "done" and tool_name == "update_plan":
            try:
                previous_plan = [dict(item) for item in state.plan]
                revised_plan = apply_plan_updates(state.plan, output.get("updates"))
                validate_plan_goal_coverage(revised_plan, state.task_intent)
                definitions_changed = any(
                    any(
                        previous.get(field) != revised.get(field)
                        for field in ("title", "description", "tool_names", "goal_ids", "kind", "completion_criteria")
                    )
                    for previous, revised in zip(previous_plan, revised_plan)
                )
                if definitions_changed and not state.flags.get("pending_plan_revision"):
                    if not reserve(state.run_budget, "replans"):
                        raise ValueError("计划修订已达到本次运行的重规划预算")
                state.plan = revised_plan
                if definitions_changed:
                    state.flags.pop("pending_plan_revision", None)
                revision = int(state.flags.get("plan_revision") or 0) + 1
                state.flags["plan_revision"] = revision
                _log(
                    state,
                    trace,
                    {
                        "node": "control",
                        "event_type": "plan_update",
                        "input": {"tool_call_id": tool_call_id, "plan_step_id": plan_step_id},
                        "output": {"steps": state.plan, "revision": revision},
                        "status": "ok",
                        "duration_ms": 0,
                    },
                    step,
                )
            except ValueError as exc:
                output = {"ok": False, "tool": tool_name, "error": str(exc)}
                status = "error"

        if status == "done" and tool_name == "finalize_answer":
            validation = validate_finalize_answer(args or {}, state)
            output = {
                **output,
                "ok": validation.valid,
                "answer_validation": validation.to_dict(),
                "validated_answer": validation.answer if validation.valid else "",
                "error_code": None if validation.valid else "answer_validation_failed",
                "error": None if validation.valid else "; ".join(validation.errors),
            }
            status = "done" if validation.valid else "error"
            if validation.valid:
                state.flags["answer_validation"] = validation.to_dict()
                state.flags["validated_answer"] = validation.answer
                state.flags["answer_finalized"] = True
                state.flags["task_completed"] = True
                state.flags["completed_goal_ids"] = [
                    str(goal.get("id"))
                    for goal in (state.task_intent.get("goals") or [])
                    if isinstance(goal, dict)
                ]
            else:
                output["answer_validation"] = record_answer_validation_failure(
                    state, args or {}, validation, phase="execution",
                )
                _consume_answer_repair(state, args or {}, validation, output, phase="execution")

        if status == "done" and output.get("ok"):
            _record_evidence(
                state, tool_name, output, tool_call_id,
                # Shared analysis adapters bind the exact snapshot version in
                # their result. Prefer it over the pre-call fingerprint.
                dataset_version=str(output.get("dataset_version") or tool_dataset_version or "") or None,
            )

        if status == "done" and tool_name == "ask_user":
            try:
                if state.plan and current_plan_step_id(state) is None:
                    raise ValueError("ask_user 只能在 running 计划步骤中调用")
                questions = normalize_questions(output.get("questions"))
                fresh_questions: list[dict[str, Any]] = []
                prefilled_answers: list[dict[str, Any]] = []
                for question in questions:
                    previous = _answered_record(state, question)
                    if previous is not None:
                        prefilled_answers.append(
                            {
                                "question_id": str(question["id"]),
                                "values": list(
                                    previous.get("values")
                                    or ([previous.get("value")] if previous.get("value") else [])
                                ),
                            }
                        )
                        continue
                    fresh_questions.append(question)

                if not fresh_questions:
                    output = {
                        "ok": True,
                        "tool": "ask_user",
                        "control": "ask_user_response",
                        "answers": prefilled_answers,
                        "note": "该问题已经由用户回答过，已复用原回答并继续执行。",
                    }
                    # Fall through to the shared result/cursor/checkpoint commit
                    # below. A special-case `continue` here left the batch cursor
                    # behind the appended tool message if the process restarted.
                    state.flags.pop("pending_user_decision", None)
                else:
                    question_resume_revision = int(state.flags.get("question_resume_revision") or 0) + 1
                    state.flags["question_resume_revision"] = question_resume_revision
                    decision_context = state.flags.get("pending_user_decision")
                    caused_by_tool_call_id = (
                        str(decision_context.get("tool_call_id") or "")
                        if isinstance(decision_context, dict)
                        else ""
                    ) or None
                    state.pending_input = {
                        "questions": fresh_questions,
                        "prefilled_answers": prefilled_answers,
                        "tool_call_id": tool_call_id,
                        "step": step,
                        "plan_step_id": plan_step_id,
                        "goal_ids": list(goal_ids),
                        "plan_revision": int(state.flags.get("plan_revision") or 0),
                        "resume_revision": question_resume_revision,
                        "caused_by_tool_call_id": caused_by_tool_call_id,
                        "batch_id": str((state.pending_tool_batch or {}).get("batch_id") or ""),
                        "batch_index": position,
                    }
                    if state.pending_tool_batch is not None:
                        state.pending_tool_batch["next_index"] = position
                    state.runtime_status = "waiting_user"
                    state.flags["runtime_status"] = state.runtime_status
                    _log(
                        state,
                        trace,
                        {
                            "node": "control",
                            "event_type": "input_required",
                            "input": {
                                "tool_call_id": tool_call_id,
                                "plan_step_id": plan_step_id,
                                "goal_ids": goal_ids,
                                "resume_revision": question_resume_revision,
                                "caused_by_tool_call_id": state.pending_input.get("caused_by_tool_call_id"),
                            },
                            "output": {
                                "questions": fresh_questions,
                                "plan_step_id": plan_step_id,
                                "goal_ids": goal_ids,
                                "resume_revision": question_resume_revision,
                                "caused_by_tool_call_id": state.pending_input.get("caused_by_tool_call_id"),
                            },
                            "status": "waiting",
                            "duration_ms": int((perf_counter() - started) * 1000),
                        },
                        step,
                    )
                    # The waiting snapshot is authoritative: persist the same
                    # question/step/goal identity before publishing its event.
                    if checkpoint_sink is not None:
                        checkpoint_sink(state.to_checkpoint())
                    if event_emitter is not None:
                        event_emitter()
                    return {"status": "waiting_user", "tool_call_id": tool_call_id}
            except ValueError as exc:
                output = {"ok": False, "tool": tool_name, "error": str(exc)}
                status = "error"

        duration_ms = int((perf_counter() - started) * 1000)
        automatic_plan_update: dict[str, Any] | None = None

        def close_reused_clarification(current_state: AgentState) -> None:
            nonlocal automatic_plan_update
            if tool_name == "ask_user" and output.get("control") == "ask_user_response":
                automatic_plan_update = _close_answered_clarification(
                    current_state,
                    tool_call_id=tool_call_id,
                    plan_step_id=plan_step_id,
                    goal_ids=goal_ids,
                )

        commit_tool_result(
            state,
            tool_call_id,
            tool_name,
            safe_args,
            output,
            status,
            duration_ms,
            batch_position=position,
            plan_step_id=plan_step_id,
            goal_ids=goal_ids,
            dataset_version=tool_dataset_version,
            after_append=close_reused_clarification,
            checkpoint_sink=checkpoint_sink,
        )
        if automatic_plan_update is not None:
            _log(
                state,
                trace,
                {
                    "node": "control",
                    "event_type": "plan_update",
                    "input": {"tool_call_id": tool_call_id, "plan_step_id": plan_step_id},
                    "output": automatic_plan_update,
                    "status": "ok",
                    "duration_ms": 0,
                },
                step,
            )
        _log(
            state,
            trace,
            {
                "node": "tool_execute",
                "event_type": "tool_rejected" if preflight_rejected else "tool_call",
                "input": {
                    "tool_call_id": tool_call_id,
                    "tool_name": tool_name,
                    "arguments": raw_arguments,
                    "plan_step_id": plan_step_id,
                },
                "output": output,
                "status": status,
                "duration_ms": duration_ms,
            },
            step,
        )
        if event_emitter is not None:
            event_emitter()
        _log(
            state,
            trace,
            {
                "node": "tool_execute",
                "event_type": "tool_completed",
                "input": {
                    "tool_call_id": tool_call_id,
                    "tool_name": tool_name,
                    "plan_step_id": plan_step_id,
                },
                "output": output,
                "status": status,
                "duration_ms": duration_ms,
            },
            step,
        )
        if event_emitter is not None:
            event_emitter()
    if state.pending_tool_batch is not None:
        state.pending_tool_batch = None
    return None


def append_user_answers(
    state: AgentState, answers: Any, *, checkpoint_sink: Any | None = None,
) -> dict[str, Any]:
    """校验并补写 ask_user 对应的 tool 消息，然后返回完成事件载荷。"""
    pending = state.pending_input
    if not pending:
        raise ValueError("当前运行没有等待用户回答")
    tool_call_id = str(pending["tool_call_id"])
    step = int(pending.get("step") or state.flags.get("steps") or 0)
    plan_step_id = str(pending.get("plan_step_id") or "") or None
    goal_ids = [str(value) for value in pending.get("goal_ids") or []]
    resume_revision = int(pending.get("resume_revision") or 0)
    caused_by_tool_call_id = str(pending.get("caused_by_tool_call_id") or "") or None
    if plan_step_id:
        if int(pending.get("plan_revision") or 0) != int(state.flags.get("plan_revision") or 0):
            raise ValueError("等待的问题已经过期，计划版本发生变化")
        step_record = next((item for item in state.plan if str(item.get("id") or "") == plan_step_id), None)
        is_clarification = bool(
            step_record
            and step_record.get("kind") == "clarification"
            and step_record.get("completion_criteria") == "user_answer"
            and str(step_record.get("status") or "") == "running"
        )
        is_data_recovery = bool(
            step_record
            and caused_by_tool_call_id
            and str((state.flags.get("pending_user_decision") or {}).get("tool_call_id") or "") == caused_by_tool_call_id
            and (
                is_bound_unsupported_recovery(state, step_record)
                or is_bound_ambiguous_series_recovery(state, step_record)
            )
        )
        if not is_clarification and not is_data_recovery:
            raise ValueError("等待的问题与当前 running clarification 步骤不匹配")
    normalized = validate_answers(list(pending.get("questions") or []), answers)
    _record_answered_inputs(
        state,
        list(pending.get("questions") or []),
        normalized,
        tool_call_id,
        plan_step_id=plan_step_id,
        goal_ids=goal_ids,
        resume_revision=resume_revision,
        caused_by_tool_call_id=caused_by_tool_call_id,
    )
    question_map = {str(item.get("id")): item for item in pending.get("questions") or []}
    context_answers = []
    for answer in normalized:
        question = question_map.get(str(answer.get("question_id") or ""), {})
        context_answers.append({
            **answer,
            "prompt": question.get("prompt"),
            "tool_call_id": tool_call_id,
            "plan_step_id": plan_step_id,
            "goal_ids": goal_ids,
            "resume_revision": resume_revision,
            "caused_by_tool_call_id": caused_by_tool_call_id,
        })
    state.task_context = apply_answer_records(state.task_context, context_answers)
    all_answers: list[dict[str, Any]] = []
    for answer in [*(pending.get("prefilled_answers") or []), *normalized]:
        if not any(item.get("question_id") == answer.get("question_id") for item in all_answers):
            all_answers.append(answer)
    output = {
        "ok": True,
        "control": "ask_user_response",
        "answers": all_answers,
        "plan_step_id": plan_step_id,
        "goal_ids": goal_ids,
        "resume_revision": resume_revision,
        "caused_by_tool_call_id": caused_by_tool_call_id,
    }
    state.flags.pop("pending_user_decision", None)
    state.pending_input = None
    state.runtime_status = "running"
    state.flags["runtime_status"] = state.runtime_status
    automatic_plan_update: dict[str, Any] | None = None

    def close_answered_step(current_state: AgentState) -> None:
        nonlocal automatic_plan_update
        automatic_plan_update = _close_answered_clarification(
            current_state,
            tool_call_id=tool_call_id,
            plan_step_id=plan_step_id,
            goal_ids=goal_ids,
        )

    commit_tool_result(
        state,
        tool_call_id,
        "ask_user",
        {"questions": pending["questions"]},
        output,
        "done",
        0,
        batch_position=int(pending.get("batch_index") or 0),
        plan_step_id=plan_step_id,
        goal_ids=goal_ids,
        after_append=close_answered_step,
        checkpoint_sink=checkpoint_sink,
    )
    return {
        "tool_call_id": tool_call_id,
        "step": step,
        "input": {"questions": pending["questions"]},
        "output": output,
        "plan_update": automatic_plan_update,
    }


def append_user_steering(state: AgentState, content: str, client_message_id: str, revision: int) -> None:
    """Apply a new user instruction at a closed boundary or supersede ask_user."""
    pending = state.pending_input
    batch = state.pending_tool_batch
    if pending and not isinstance(batch, dict):
        raise ValueError("等待状态缺少待完成工具批次")
    if pending:
        calls = list(batch.get("calls") or [])
        start = int(batch.get("next_index") or 0)
        for index in range(start, len(calls)):
            call = calls[index]
            call_id = str(call.get("id") or "")
            function = call.get("function") or {}
            name = str(function.get("name") or "")
            already = any(str(item.get("tool_call_id") or "") == call_id for item in state.tool_results)
            if already:
                continue
            result = ({"ok": True, "control": "ask_user_superseded", "answers": [],
                       "note": "用户在等待期间发送了新指令，原问题已作废。"}
                      if call_id == str(pending.get("tool_call_id") or "") else
                      {"ok": False, "error_code": "superseded_by_user_input",
                       "error": "该调用因用户在等待期间发送新指令而取消。"})
            state.add_tool_result(call_id, name, {}, result, "done" if result.get("ok") else "error", 0)
        state.pending_tool_batch = None
        state.pending_input = None
    state.flags["input_revision"] = max(int(state.flags.get("input_revision") or 0), int(revision))
    from backend.agent_runtime.task_context import apply_answer_records
    previous_revision = int(state.task_context.get("revision") or 0)
    updated = apply_answer_records(state.task_context, [{
        "question_id": f"steering-{client_message_id}",
        "prompt": "用户主动纠正的约束（预算/budget）",
        "values": [str(content)],
        "tool_call_id": f"message:{client_message_id}",
    }])
    if int(updated.get("revision") or 0) == previous_revision:
        updated["revision"] = previous_revision + 1
        updated["blockers"] = [*list(updated.get("blockers") or []),
                                "用户纠正尚未解析为受支持的结构化筛选条件，需先询问确认"]
        state.flags["pending_user_decision"] = True
    else:
        state.flags.pop("pending_user_decision", None)
    state.task_context = updated
    for flag in ("answer_finalized", "validated_answer", "answer_validation", "answer_validation_failed"):
        state.flags.pop(flag, None)
    state.final_message = None
    applied = list(state.flags.get("applied_client_message_ids") or [])
    applied.append(str(client_message_id))
    state.flags["applied_client_message_ids"] = applied[-100:]
    state.append_message({"role": "user", "content": str(content)})


def context_update_node(state: AgentState, trace: RunTrace, step: int) -> None:
    successful = sum(1 for item in state.tool_results if item.get("status") == "done")
    state.flags["successful_tool_count"] = successful
    state.flags["tool_count"] = len(state.tool_results)
    state.flags["status"] = "awaiting_model"
    _log(
        state,
        trace,
        {
            "node": "context_update",
            "event_type": "context",
            "input": {"tool_count": len(state.tool_results)},
            "output": {
                "successful_tool_count": successful,
                "message_count": len(state.messages),
            },
            "status": "ok",
            "duration_ms": 0,
        },
        step,
    )


def finalize_node(
    state: AgentState,
    trace: RunTrace,
    final_text: str,
    source: str = "agent",
) -> None:
    state.final_message = {
        "role": "assistant",
        "content": final_text,
        "source": source,
        "tool_results": state.tool_results,
    }
    state.flags["summary_source"] = source
    _log(
        state,
        trace,
        {
            "node": "finalize",
            "event_type": "final",
            "input": {"tool_count": len(state.tool_results)},
            "output": {"content": final_text, "source": source, "preview": True},
            "status": "candidate",
            "duration_ms": 0,
        },
        int(state.flags.get("steps", 0)),
    )
