"""单模型、多轮原生 tool_calls Agent Loop。"""

from __future__ import annotations

from pathlib import Path
import json
import threading
import time
from uuid import uuid4
from typing import Any, Callable

from backend.agent_runtime.nodes import (
    SYSTEM_PROMPT,
    _log,
    agent_execute_node,
    append_user_answers,
    append_user_steering,
    context_update_node,
    finalize_node,
    plan_is_complete,
    route_node,
    tool_execute_node,
    _empty_recovery_attempts_for_step,
)
from backend.agent_runtime.context_budget import DEFAULT_CONTEXT_BUDGET, ContextBudget, TokenEstimator
from backend.agent_runtime.prefix import build_prefix_context
from backend.agent_runtime.report_projection import build_report
from backend.agent_runtime.report_context import bind_report_access, prepare_report_followup
from backend.agent_runtime.completion_policy import decide_completion, plan_ready_for_completion
from backend.agent_runtime.mcp import MCPManager
from backend.agent_runtime.session_store import SessionStore
from backend.agent_runtime.message_protocol import MessageProtocolError, validate_message_protocol
from backend.agent_runtime.skills import discover_skills, skill_route_context
from backend.agent_runtime.state import AgentState
from backend.agent_runtime.tool_registry import stable_tool_schemas
from backend.agent_runtime.execution import ExecutionContext, ExecutionBoundaryError, check_execution
from backend.agent_runtime.trace import RunTrace
from backend.config import DEEPSEEK_MODEL, ROOT
from backend.config import (SCHEDULER_RUN_TIMEOUT_S, RUN_BUDGET_LIMITS,
                            MIDRUN_INPUT_ENABLED, RUN_RECOVERY_ENABLED)
from backend.agent_runtime.run_budget import new_run_budget, reserve, add_active_seconds
from backend.agent_runtime.answer_validation import deterministic_answer_from_evidence
from backend.agent_runtime.task_context import initial_task_context, context_summary
from backend.deepseek_client import get_client


def _emit_new_events(
    trace: RunTrace,
    event_sink: Callable[[dict[str, Any]], None] | None,
    cursor: int,
) -> int:
    if event_sink is None:
        return len(trace.events)
    for event in trace.events[cursor:]:
        try:
            event_sink(event)
        except Exception as exc:
            # 非关键进度落盘不能改变 Agent 语义；保留去敏诊断供最终trace检查。
            trace.record(
                "telemetry",
                "event_sink_error",
                {"event_type": str(event.get("event_type") or "event")},
                {"error_type": type(exc).__name__},
                "degraded",
            )
    return len(trace.events)


def _sync_task_context_message(state: AgentState) -> None:
    marker = "[TASK_CONTEXT_SUMMARY]"
    state.messages = [
        message for index, message in enumerate(state.messages)
        if index == 0 or not (message.get("role") == "system" and str(message.get("content") or "").startswith(marker))
    ]
    content = marker + "\n" + context_summary(state.task_context)
    task_intent = state.task_intent or {}
    if task_intent:
        complexity = "多目标" if task_intent.get("complexity") == "multi_goal" or len(task_intent.get("goals") or []) > 1 else "单目标"
        content += "\n\n已确认的任务合同（所有新任务都必须发布可见计划）："
        content += f"\n目标复杂度：{complexity}；计划要求：必须"
        for goal in task_intent.get("goals") or []:
            if isinstance(goal, dict):
                content += (
                    f"\n- goal_id={goal.get('id')}: {str(goal.get('description') or '')[:300]}"
                    f"；交付物={str(goal.get('deliverable') or '')[:200]}"
                )
        if state.plan:
            content += "\n当前计划（面向用户展示；不得输出隐藏推理）："
            for item in state.plan:
                if isinstance(item, dict):
                    content += (
                        f"\n- [{item.get('status') or 'queued'}] {str(item.get('title') or '')[:120]}"
                        f"（{item.get('kind') or 'stage'}；完成条件={item.get('completion_criteria') or 'tool_result'}）"
                    )
        else:
            content += "\n尚未发布计划：必须先调用 set_plan，不能先运行数据工具或直接提交答案。"
        if state.flags.get("pending_plan_revision"):
            content += "\n新追加的目标尚未进入计划：先用 update_plan 修订 queued 步骤并覆盖全部目标。"
    pending_decision = state.flags.get("pending_user_decision")
    if isinstance(pending_decision, dict) and pending_decision.get("reason_code") == "unsupported_filter":
        content += (
            "\n数据工具报告 unsupported_filter。当前只允许 ask_user 针对失败条件请求用户决定；"
            "回答会关联原始数据调用、当前计划步骤和目标。回答本身不能完成数据阶段，必须按确认后的条件重新执行数据工具。"
        )
    current = _current_evidence(state)
    if current:
        content += (
            "\n\n已有数据证据。数据结论必须调用 finalize_answer，以 schema_version=2 提交；"
            "使用 fact_ref 引用 evidence_id，推荐结果使用 recommendation_table 引用实体组。"
            "不要在 text 块重复填写数值、单位、车型名或筛选条件。"
        )
        content += "\n当前可引用证据（evidence_id | metric | value | unit | scope | sample_size | goal_ids | plan_step_id）："
        for record in current[:32]:
            fields = (
                record.get("evidence_id"), record.get("metric"), record.get("value"),
                record.get("unit"), record.get("scope"), record.get("sample_size"),
                ",".join(map(str, record.get("goal_ids") or [])), record.get("plan_step_id"),
            )
            content += "\n- " + " | ".join("未知" if value is None else str(value)[:160] for value in fields)
    state.append_message({"role": "system", "content": content})


def _close_interrupted_tool_tail(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Close only a valid unfinished tool-call suffix from an older run."""
    try:
        validate_message_protocol(history)
        return history
    except MessageProtocolError:
        # The relaxed validator accepts exactly one case: an otherwise valid
        # protocol whose final assistant tool-call batch is still outstanding.
        # Other malformed histories continue to fail here.
        validate_message_protocol(history, allow_pending_tail=True)

    assistant_index = next(
        (index for index in range(len(history) - 1, -1, -1)
         if history[index].get("role") == "assistant" and history[index].get("tool_calls")),
        None,
    )
    if assistant_index is None:
        return history
    calls = history[assistant_index].get("tool_calls") or []
    completed_ids = {
        str(message.get("tool_call_id") or "")
        for message in history[assistant_index + 1:]
        if message.get("role") == "tool"
    }
    interrupted = []
    for call in calls:
        call_id = str(call.get("id") or "") if isinstance(call, dict) else ""
        if call_id and call_id not in completed_ids:
            function = call.get("function") if isinstance(call, dict) else None
            name = str(function.get("name") or "") if isinstance(function, dict) else ""
            interrupted.append({
                "role": "tool",
                "tool_call_id": call_id,
                "name": name,
                "content": json.dumps({
                    "ok": False,
                    "error_code": "previous_run_interrupted",
                    "error": "此前运行已结束，该调用未完成；不可作为成功结果或数据证据。",
                }, ensure_ascii=False),
            })
    repaired = [*history, *interrupted]
    validate_message_protocol(repaired)
    return repaired


def _current_evidence(state: AgentState) -> list[dict[str, Any]]:
    revision = state.task_context.get("revision", 0)
    return [item for item in state.evidence_records if item.get("constraint_revision") == revision]


def _record_finalize_protocol_reminder(state: AgentState) -> None:
    """Track a missing finalize call separately from bounded answer repairs."""
    state.flags["finalize_protocol_reminders"] = int(
        state.flags.get("finalize_protocol_reminders") or 0
    ) + 1


def _effective_confirmed_filters(state: AgentState, fallback: dict[str, Any] | None) -> dict[str, Any]:
    result = dict(fallback or {})
    for key, record in (state.task_context.get("constraints") or {}).items():
        if isinstance(record, dict) and record.get("status") == "confirmed":
            result[str(key)] = record.get("value")
    return result


def _current_report_result(state: AgentState, result: dict[str, Any]) -> dict[str, Any]:
    current_revision = int(state.task_context.get("revision") or 0)
    domain_tools = {"recommend", "price_compare", "region_gap", "price_decay", "csv_query"}
    tool_results = [
        item for item in result.get("tool_results") or []
        if str(item.get("tool_name") or item.get("tool") or "") not in domain_tools
        or int(item.get("constraint_revision", 0)) == current_revision
    ]
    return {**result, "tool_results": tool_results,
            "facts": [item.get("output") for item in tool_results]}


def _apply_deterministic_answer_fallback(state: AgentState, validation: Any) -> bool:
    """Commit a validated deterministic answer through the normal completion contract."""
    if validation is None or not validation.valid or not state.plan or state.plan[-1].get("kind") != "finalize":
        return False
    prior_validation = state.flags.get("answer_validation")
    goal_ids = [str(goal.get("id")) for goal in (state.task_intent.get("goals") or []) if isinstance(goal, dict)]
    call_id = "runtime-fallback-" + str((prior_validation or {}).get("candidate_fingerprint") or uuid4().hex)[:24]
    state.flags["answer_validation"] = validation.to_dict()
    state.flags["answer_last_invalid_validation"] = prior_validation
    state.flags["validated_answer"] = validation.answer
    state.flags["answer_finalized"] = True
    state.flags["answer_validation_failed"] = False
    state.flags["task_completed"] = True
    state.flags["completed_goal_ids"] = goal_ids
    state.flags["answer_validation_fallback"] = True
    state.tool_results.append({
        "tool_call_id": call_id, "tool_name": "finalize_answer", "status": "done",
        "plan_step_id": str(state.plan[-1].get("id") or ""), "goal_ids": goal_ids,
        "constraint_revision": int((state.task_context or {}).get("revision") or 0),
        "output": {"ok": True, "validated_answer": validation.answer,
                   "answer_validation": validation.to_dict(), "fallback": "deterministic"},
    })
    return True


def run_agent(
    user_text: str,
    max_steps: int = 12,
    session_id: str | None = None,
    run_id: str | None = None,
    session_store: SessionStore | None = None,
    event_sink: Callable[[dict[str, Any]], None] | None = None,
    reuse_current_user: bool = False,
    skills_root: Path | str | None = None,
    mcp_manager: MCPManager | None = None,
    confirmed_filters: dict[str, Any] | None = None,
    context_id: str | None = None,
    resume_checkpoint: dict[str, Any] | None = None,
    input_answers: list[dict[str, Any]] | None = None,
    owner_id: str | None = None,
    context_budget: ContextBudget | None = None,
    token_estimator: TokenEstimator | None = None,
    cancellation: threading.Event | None = None,
    execution_context: ExecutionContext | None = None,
    checkpoint_sink: Callable[[dict[str, Any]], None] | None = None,
    message_source: Callable[[], list[dict[str, Any]]] | None = None,
    message_ack: Callable[[list[dict[str, Any]], dict[str, Any]], bool] | None = None,
    steering_message: dict[str, Any] | None = None,
    lease_token: str | None = None,
    execution_attempt: int | None = None,
    defer_session_commit: bool = False,
    source_report_id: str | None = None,
) -> dict[str, Any]:
    context_budget = context_budget or DEFAULT_CONTEXT_BUDGET
    token_estimator = token_estimator or TokenEstimator()
    resume_mode = resume_checkpoint is not None
    original_user_text = str(user_text or "").strip()
    effective_context_id = str(context_id or session_id or "").strip() or None
    effective_user_text = original_user_text
    if resume_mode:
        state = AgentState.from_checkpoint(resume_checkpoint or {})
        saved_budget = state.flags.get("context_budget_config")
        if isinstance(saved_budget, dict):
            context_budget = ContextBudget.from_config(saved_budget)
        else:
            state.flags["context_budget_config"] = context_budget.to_config()
        if not isinstance(state.flags.get("current_run_start"), int):
            current_user = next(
                (index for index in range(len(state.messages) - 1, 0, -1) if state.messages[index].get("role") == "user"),
                0,
            )
            state.flags["current_run_start"] = current_user
        original_user_text = str(state.flags.get("original_user_text") or state.user_text).strip()
        effective_user_text = state.user_text
        saved_cursor = state.flags.get("session_message_cursor")
        history_length = (
            max(0, int(saved_cursor))
            if isinstance(saved_cursor, int) and not isinstance(saved_cursor, bool)
            else sum(1 for message in state.dynamic_messages() if message.get("role") != "system")
        )
        if input_answers is None and state.pending_input is not None:
            if steering_message is None:
                raise ValueError("恢复等待中的 Agent 必须提供 input_answers")
            append_user_steering(
                state, str(steering_message.get("content") or ""),
                str(steering_message.get("client_message_id") or ""),
                int(steering_message.get("revision") or 0),
            )
            if message_ack is not None and not message_ack([steering_message], state.to_checkpoint()):
                raise RuntimeError("steering_message_checkpoint_conflict")
            answer_completion = None
        else:
            answer_completion = append_user_answers(
                state, input_answers, checkpoint_sink=checkpoint_sink,
            ) if input_answers is not None else None
    else:
        answer_completion = None
        if confirmed_filters:
            effective_user_text = (
                f"{original_user_text}\n\n"
                "用户已确认的动态筛选条件（只能作为当前问题上下文，必须以工具实际支持字段为准）：\n"
                f"{json.dumps(confirmed_filters, ensure_ascii=False, sort_keys=True)}"
            )
        store = session_store or (SessionStore() if session_id else None)
        if store and effective_context_id and owner_id is not None:
            # Claim a new context before the strict owned-history read. Both
            # stores reject an existing context owned by another principal.
            store.append_messages(effective_context_id, [], owner_id=owner_id)
        history = store.active_messages(effective_context_id, owner_id=owner_id) if store and effective_context_id else []
        history_length = sum(1 for message in history if message.get("role") != "system")
        history = _close_interrupted_tool_tail(history)
        state = AgentState.create(
            effective_user_text,
            SYSTEM_PROMPT,
            history=history,
            append_user=not (
                reuse_current_user
                and bool(history)
                and history[-1].get("role") == "user"
                and history[-1].get("content") == effective_user_text
            ),
        )
        state.flags["original_user_text"] = original_user_text
        state.flags["context_budget_config"] = context_budget.to_config()
    state.flags.setdefault("session_message_cursor", history_length)
    confirmed_filters = _effective_confirmed_filters(state, confirmed_filters)
    if not state.task_context:
        state.task_context = initial_task_context(original_user_text, confirmed_filters)
    elif confirmed_filters:
        for key, value in confirmed_filters.items():
            state.task_context.setdefault("constraints", {}).setdefault(
                str(key), {"value": value, "source": "confirmed_filters", "status": "confirmed"}
            )
    if resume_mode:
        store = session_store or (SessionStore() if session_id else None)
        history = store.active_messages(effective_context_id, owner_id=owner_id) if store and effective_context_id else []
        state.flags["original_user_text"] = original_user_text
    report_access, report_context_text = prepare_report_followup(
        store, owner_id, effective_context_id, original_user_text,
        state=state, max_chars=max(300, min(6000, context_budget.input_tokens // 5)),
        source_report_id=source_report_id,
    )
    report_context_messages = ([{"role": "system", "content": report_context_text}]
                               if report_context_text else [])
    active_source_report_id = str(source_report_id or state.flags.get("active_source_report_id") or "").strip()
    if active_source_report_id:
        state.flags["active_source_report_id"] = active_source_report_id
    skill_routes = discover_skills(skills_root or ROOT)
    defer_skill_routes = bool(resume_mode and state.pending_tool_batch is not None)
    if skill_routes and not defer_skill_routes:
        state.append_message({"role": "system", "content": skill_route_context(skill_routes)})
        state.flags["skill_routes"] = [route.name for route in skill_routes]
    state.flags["max_steps"] = max(0, int(max_steps))
    state.flags["session_id"] = session_id
    effective_run_id = str(run_id or state.trace_id)
    state.flags["run_id"] = effective_run_id
    if not state.run_budget:
        state.run_budget = new_run_budget(RUN_BUDGET_LIMITS)
        state.run_budget["model_calls"] = int((state.flags.get("token_usage") or {}).get("model_calls") or 0)
        state.run_budget["tool_executions"] = len(state.tool_results)
    segment_started = time.monotonic()
    budget_closed = False

    def close_budget_segment() -> None:
        nonlocal budget_closed
        if not budget_closed:
            add_active_seconds(state.run_budget, time.monotonic() - segment_started)
            budget_closed = True
    if execution_context is None:
        execution_context = ExecutionContext(
            run_id=effective_run_id, segment_id=uuid4().hex,
            cancellation=cancellation or threading.Event(),
            deadline_monotonic=time.monotonic() + min(
                SCHEDULER_RUN_TIMEOUT_S,
                max(0.001, float(state.run_budget["limits"]["active_execution_seconds"])
                    - float(state.run_budget.get("active_execution_seconds") or 0)),
            ),
        )
    mcp_tools = mcp_manager.snapshot_tools() if mcp_manager is not None else []
    prefix = build_prefix_context(
        DEEPSEEK_MODEL,
        SYSTEM_PROMPT,
        stable_tool_schemas(mcp_tools),
    )
    state.flags["prefix_hash"] = prefix.prefix_hash
    state.flags["prefix_cache_hit"] = prefix.cache_hit
    trace = RunTrace.create(state.trace_id, effective_run_id, execution_context.segment_id)
    event_cursor = 0
    if not resume_mode:
        route_node(state, trace)
    else:
        state.flags["status"] = "ready"
        state.runtime_status = "running"
        if answer_completion is not None:
            _log(
                state,
                trace,
                {
                    "node": "control",
                    "event_type": "tool_completed",
                    "input": {
                        "tool_call_id": answer_completion["tool_call_id"],
                        "tool_name": "ask_user",
                        "arguments": json.dumps(answer_completion["input"], ensure_ascii=False),
                    },
                    "output": answer_completion["output"],
                    "status": "done",
                    "duration_ms": 0,
                },
                int(answer_completion["step"]),
            )
            if answer_completion.get("plan_update"):
                _log(
                    state,
                    trace,
                    {
                        "node": "control",
                        "event_type": "plan_update",
                        "input": {
                            "tool_call_id": answer_completion["tool_call_id"],
                            "plan_step_id": (answer_completion.get("output") or {}).get("plan_step_id"),
                        },
                        "output": answer_completion["plan_update"],
                        "status": "ok",
                        "duration_ms": 0,
                    },
                    int(answer_completion["step"]),
                )
    event_cursor = _emit_new_events(trace, event_sink, event_cursor)

    # An answered ask_user closes its tool call, then the rest of the original
    # assistant batch resumes before another model request is allowed.
    if resume_mode and state.pending_tool_batch is not None:
        def emit_resumed_tool_event() -> None:
            nonlocal event_cursor
            event_cursor = _emit_new_events(trace, event_sink, event_cursor)

        batch_calls = list(state.pending_tool_batch.get("calls") or [])
        with bind_report_access(report_access):
            control_signal = tool_execute_node(
                state, trace, batch_calls, int(state.pending_tool_batch.get("step") or 1),
                mcp_manager=mcp_manager, event_emitter=emit_resumed_tool_event,
                run_id=effective_run_id, cancellation=cancellation,
                execution_context=execution_context,
                checkpoint_sink=checkpoint_sink,
            )
        event_cursor = _emit_new_events(trace, event_sink, event_cursor)
        if control_signal and control_signal.get("status") == "waiting_user":
            close_budget_segment()
            state.flags["status"] = "waiting_user"
            return _waiting_result(
                store, effective_context_id, state, history_length,
                _persistence_run_id(effective_run_id, resume_mode, state, execution_attempt),
                owner_id, lease_token, defer_session_commit,
                original_user_text, effective_run_id, confirmed_filters,
            )
        if defer_skill_routes and skill_routes:
            state.append_message({"role": "system", "content": skill_route_context(skill_routes)})
            state.flags["skill_routes"] = [route.name for route in skill_routes]

    client = get_client()
    final_text = ""
    final_source = "agent"

    if not state.user_text:
        final_text = "请输入你想分析的二手车问题。"
        final_source = "runtime"
    elif max_steps < 1:
        state.flags["max_steps_exceeded"] = True
        final_text = "已达到本次分析的最大执行步数，暂未生成完整回答。"
        final_source = "runtime"
    elif not client.is_available():
        state.flags["status"] = "model_unavailable"
        final_text = f"当前模型不可用，无法启动 Agent：{client.reason}"
        final_source = "runtime"
    else:
        for step in range(1, max_steps + 1):
            if message_source is not None:
                queued_messages = message_source()
                if queued_messages:
                    for queued in queued_messages:
                        message_id = str(queued.get("client_message_id") or "")
                        if message_id in (state.flags.get("applied_client_message_ids") or []):
                            continue
                        queued_source_report_id = str(queued.get("source_report_id") or "").strip()
                        if queued_source_report_id and queued_source_report_id != active_source_report_id:
                            report_access, report_context_text = prepare_report_followup(
                                store, owner_id, effective_context_id,
                                str(queued.get("content") or ""), state=state,
                                max_chars=max(300, min(6000, context_budget.input_tokens // 5)),
                                source_report_id=queued_source_report_id,
                            )
                            report_context_messages = (
                                [{"role": "system", "content": report_context_text}]
                                if report_context_text else []
                            )
                            active_source_report_id = queued_source_report_id
                            state.flags["active_source_report_id"] = active_source_report_id
                        append_user_steering(
                            state, str(queued.get("content") or ""), message_id,
                            int(queued.get("revision") or 0),
                        )
                    if message_ack is not None:
                        if not message_ack(queued_messages, state.to_checkpoint()):
                            raise RuntimeError("steering_message_checkpoint_conflict")
            if checkpoint_sink is not None:
                checkpoint_sink(state.to_checkpoint())
            try:
                check_execution(execution_context)
            except ExecutionBoundaryError as exc:
                close_budget_segment()
                return _runtime_result(
                    state, original_user_text, effective_context_id, effective_run_id,
                    confirmed_filters, status="cancelled" if exc.code == "cancelled" else "error",
                    summary=str(exc),
                )
            if cancellation is not None and cancellation.is_set():
                close_budget_segment()
                return _runtime_result(
                    state,
                    original_user_text,
                    effective_context_id,
                    effective_run_id,
                    confirmed_filters,
                    status="cancelled",
                    summary="Agent 运行已取消。",
                )
            state.flags["steps"] = step
            _sync_task_context_message(state)
            if not reserve(state.run_budget, "model_calls"):
                state.flags["budget_exhausted"] = "model_calls"
                final_text = "已达到整个分析任务的模型调用预算，未能完成答案校验与交付。"
                final_source = "runtime"
                break
            with bind_report_access(report_access):
                model_result = agent_execute_node(
                    state,
                    trace,
                    client,
                    step,
                    prefix=prefix,
                    context_budget=context_budget,
                    token_estimator=token_estimator,
                    execution_context=execution_context,
                    context_messages=report_context_messages,
                )
            event_cursor = _emit_new_events(trace, event_sink, event_cursor)
            try:
                check_execution(execution_context)
            except ExecutionBoundaryError as exc:
                close_budget_segment()
                return _runtime_result(
                    state, original_user_text, effective_context_id, effective_run_id,
                    confirmed_filters, status="cancelled" if exc.code == "cancelled" else "error",
                    summary=str(exc),
                )
            if model_result["error"]:
                state.flags["status"] = "model_error"
                final_text = (
                    "context_input_too_large"
                    if model_result.get("error_code") == "context_input_too_large"
                    else f"模型调用失败，暂时无法完成分析：{model_result['error']}"
                )
                final_source = "runtime"
                break

            assistant = model_result["assistant"] or {}
            tool_calls = assistant.get("tool_calls") or []
            if not tool_calls:
                if not state.task_intent:
                    state.append_message({
                        "role": "system",
                        "content": "本轮还没有提交任务目标。请先调用 declare_task，逐项声明用户要求的交付目标；每个任务都必须发布计划。",
                    })
                    continue
                if not state.plan:
                    state.append_message({
                        "role": "system",
                        "content": "任务目标已声明但当前计划为空。必须调用 set_plan 发布简短全程计划；直接文本和数据工具都不能替代计划。",
                    })
                    continue
                active_step = next((item for item in state.plan if item.get("status") == "running"), None)
                if state.flags.get("pending_plan_revision"):
                    state.append_message({
                        "role": "system",
                        "content": "新追加的目标尚未进入计划。请用 update_plan 调整 queued 步骤的定义并绑定全部目标；不能改写 complete 或 running 步骤。",
                    })
                    continue
                if active_step is None:
                    next_step = next((item for item in state.plan if item.get("status") == "queued"), None)
                    if next_step is not None:
                        state.append_message({
                            "role": "system",
                            "content": f"没有 running 步骤。请调用 update_plan，将下一步 {next_step.get('id')} 设为 running。",
                        })
                        continue
                    final_text = "计划状态异常：没有可执行步骤或待提交的最终阶段。"
                    final_source = "runtime"
                    state.flags["plan_state_invalid"] = True
                    break
                if active_step and active_step.get("kind") != "finalize":
                    state.append_message({
                        "role": "system",
                        "content": (
                            f"当前计划步骤 {active_step.get('id')} 尚未收口。"
                            "若其绑定结果已成功，调用 update_plan 将该步骤设为 complete，并按顺序启动下一步；"
                            "若需要用户决定，在 clarification 步骤调用 ask_user。"
                        ),
                    })
                    continue
                if state.evidence_records and not state.flags.get("answer_finalized"):
                    _record_finalize_protocol_reminder(state)
                    state.append_message({
                        "role": "system",
                        "content": "所有前置阶段已收口，请调用 finalize_answer 提交 schema_version=2 的结构化答案并覆盖全部 goal_ids。",
                    })
                    continue
                if not state.flags.get("task_completed"):
                    _record_finalize_protocol_reminder(state)
                    state.append_message({
                        "role": "system",
                        "content": "任务尚未通过受控完成校验，不能直接输出文本；请按当前 finalize 步骤提交并核验答案。",
                    })
                    continue
                final_text = str(assistant.get("content") or "Agent 未返回最终回答。")
                break

            def emit_tool_event() -> None:
                nonlocal event_cursor
                event_cursor = _emit_new_events(trace, event_sink, event_cursor)

            state.pending_tool_batch = {
                "batch_id": uuid4().hex,
                "assistant_message_index": len(state.messages) - 1,
                "calls": tool_calls,
                "next_index": 0,
                "completed_call_ids": [],
                "step": step,
            }
            if checkpoint_sink is not None:
                checkpoint_sink(state.to_checkpoint())

            with bind_report_access(report_access):
                control_signal = tool_execute_node(
                    state,
                    trace,
                    tool_calls,
                    step,
                    mcp_manager=mcp_manager,
                    event_emitter=emit_tool_event,
                    run_id=effective_run_id,
                    cancellation=cancellation,
                    execution_context=execution_context,
                    checkpoint_sink=checkpoint_sink,
                )
            event_cursor = _emit_new_events(trace, event_sink, event_cursor)
            if control_signal and control_signal.get("status") == "waiting_user":
                close_budget_segment()
                state.flags["status"] = "waiting_user"
                return _waiting_result(
                    store, effective_context_id, state, history_length,
                    _persistence_run_id(effective_run_id, resume_mode, state, execution_attempt),
                    owner_id, lease_token, defer_session_commit,
                    original_user_text, effective_run_id, confirmed_filters,
                )
            if state.flags.get("answer_finalized"):
                final_text = str(state.flags.get("validated_answer") or "")
                final_source = "validated"
                break
            if state.flags.get("answer_repair_exhausted"):
                prior_validation = state.flags.get("answer_validation")
                fallback = deterministic_answer_from_evidence(state)
                if _apply_deterministic_answer_fallback(state, fallback):
                    final_text = fallback.answer
                    final_source = "validated"
                else:
                    state.flags["answer_validation_failed"] = True
                    state.flags["answer_fallback_unavailable"] = True
                    state.flags["answer_validation"] = {
                        **(prior_validation if isinstance(prior_validation, dict) else {}),
                        "fallback_gaps": list(state.flags.get("answer_fallback_gaps") or []),
                    }
                    final_text = "已完成数据查询，但最终答案未通过完整性校验；存在未满足的目标、证据绑定或条件版本要求。"
                    final_source = "runtime"
                break
            active_data_step = next(
                (item for item in state.plan if item.get("kind") == "data" and item.get("status") == "running"),
                None,
            )
            if active_data_step and _empty_recovery_attempts_for_step(state, active_data_step) > 2:
                state.flags["empty_recovery_exhausted"] = True
                state.flags["empty_recovery_exhausted_scope"] = {
                    "plan_step_id": str(active_data_step.get("id") or ""),
                    "goal_ids": list(active_data_step.get("goal_ids") or []),
                    "constraint_revision": int((state.task_context or {}).get("revision") or 0),
                }
                has_current_evidence = bool(_current_evidence(state))
                if has_current_evidence:
                    final_text = (
                        "当前计划步骤在保留已确认条件的情况下连续返回空结果，已停止自动重试。"
                        "已有有效数据仍然成立，但部分目标尚未完成；请确认是否要调整当前车型或筛选条件。"
                    )
                else:
                    final_text = "当前计划步骤在保留已确认条件的情况下连续返回空结果，已停止自动重试；请确认车型或筛选条件。"
                final_source = "agent"
                break
            context_update_node(state, trace, step)
            event_cursor = _emit_new_events(trace, event_sink, event_cursor)
        else:
            state.flags["max_steps_exceeded"] = True
            final_text = "已达到本次分析的最大执行步数，未能生成完整回答。"
            final_source = "runtime"

    try:
        check_execution(execution_context)
    except ExecutionBoundaryError as exc:
        close_budget_segment()
        return _runtime_result(
            state, original_user_text, effective_context_id, effective_run_id,
            confirmed_filters, status="cancelled" if exc.code == "cancelled" else "error",
            summary=str(exc),
        )
    execution_status = str(state.flags.get("status") or "")
    if state.flags.get("answer_validation_failed"):
        final_text = "数据已查询，但最终答案未通过校验。"
        final_source = "runtime"
    finalize_node(state, trace, final_text, final_source)
    event_cursor = _emit_new_events(trace, event_sink, event_cursor)
    check_execution(execution_context)
    session_messages = _dynamic_session_messages(state, history_length)
    persistence_id = _persistence_run_id(effective_run_id, resume_mode, state, execution_attempt)
    close_budget_segment()
    candidate = _runtime_result(
        state,
        original_user_text,
        effective_context_id,
        effective_run_id,
        confirmed_filters,
        status="candidate",
        summary=(
            "数据已查询，但最终答案未通过校验。"
            if state.flags.get("answer_validation_failed")
            else final_text
        ),
        summary_source=final_source,
        trace=trace,
        tool_schemas=prefix.tools,
        session_id=session_id,
    )
    evidence_is_stale = bool(state.evidence_records) and not _current_evidence(state)
    report = None
    projection_error = None
    if (plan_is_complete(state) or plan_ready_for_completion(state)) and not evidence_is_stale:
        try:
            report_result = _current_report_result(state, candidate)
            report = build_report(user_text=original_user_text, result=report_result)
        except Exception as exc:  # noqa: BLE001 - turn projection failures into a typed terminal failure
            projection_error = str(exc)
    outcome = decide_completion(
        state,
        candidate,
        execution_status=execution_status,
        report=report,
        projection_error=projection_error,
        evidence_is_stale=evidence_is_stale,
    )
    succeeded = outcome.kind == "completed"
    if succeeded:
        validated_text = str((state.final_message or {}).get("content") or final_text).strip()
        if validated_text:
            already_present = any(
                message.get("role") == "assistant" and str(message.get("content") or "").strip() == validated_text
                for message in session_messages
            )
            if not already_present:
                session_messages.append({"role": "assistant", "content": validated_text})
    if succeeded and plan_ready_for_completion(state):
        # The model may submit while this step is running. Only the runtime,
        # after answer validation and completion checks pass, closes it.
        state.plan[-1]["status"] = "complete"
        state.plan[-1].pop("reason", None)
        state.flags["plan_revision"] = int(state.flags.get("plan_revision") or 0) + 1
        state.flags["final_plan_closed"] = True
        _log(state, trace, {
            "node": "control", "event_type": "plan_update",
            "input": {"plan_step_id": state.plan[-1]["id"]},
            "output": {"steps": state.plan, "revision": state.flags["plan_revision"]},
            "status": "ok", "duration_ms": 0,
        }, int(state.flags.get("steps") or 0))
        event_cursor = _emit_new_events(trace, event_sink, event_cursor)
    state.flags["status"] = "done" if succeeded else "error"
    state.runtime_status = "done" if succeeded else "error"
    result = _runtime_result(
        state,
        original_user_text,
        effective_context_id,
        effective_run_id,
        confirmed_filters,
        status="done" if succeeded else "error",
        summary=final_text,
        summary_source=final_source,
        trace=trace,
        tool_schemas=prefix.tools,
        session_id=session_id,
    )
    if succeeded:
        result["report"] = report
        if not defer_session_commit:
            _persist_dynamic_messages(
                store, effective_context_id, state, history_length, persistence_id, owner_id, lease_token,
                messages=session_messages,
            )
        elif store and effective_context_id:
            result["_session_commit"] = {"messages": session_messages, "run_id": persistence_id}
    else:
        result["failure_code"] = outcome.failure_code
        result["retryable"] = outcome.retryable
        if projection_error:
            result["projection_error"] = projection_error[:500]
    return result


def _persist_dynamic_messages(
    store: SessionStore | None,
    context_id: str | None,
    state: AgentState,
    history_length: int,
    run_id: str,
    owner_id: str | None,
    lease_token: str | None = None,
    *,
    messages: list[dict[str, Any]] | None = None,
) -> None:
    if not store or not context_id:
        return
    new_messages = messages if messages is not None else _dynamic_session_messages(state, history_length)
    store.append_messages(context_id, new_messages, run_id=run_id, owner_id=owner_id, lease_token=lease_token)


def _dynamic_session_messages(state: AgentState, history_length: int) -> list[dict[str, Any]]:
    messages = [message for message in state.dynamic_messages() if message.get("role") != "system"]
    return messages[max(0, int(history_length)):]


def _waiting_result(
    store: SessionStore | None,
    context_id: str | None,
    state: AgentState,
    history_length: int,
    persistence_id: str,
    owner_id: str | None,
    lease_token: str | None,
    defer_session_commit: bool,
    original_user_text: str,
    run_id: str,
    confirmed_filters: dict[str, Any] | None,
) -> dict[str, Any]:
    messages = _dynamic_session_messages(state, history_length)
    state.flags["session_message_cursor"] = history_length + len(messages)
    result = _runtime_result(
        state, original_user_text, context_id, run_id, confirmed_filters, status="waiting_user",
    )
    if defer_session_commit and store and context_id:
        result["_session_commit"] = {"messages": messages, "run_id": persistence_id}
    else:
        _persist_dynamic_messages(store, context_id, state, history_length, persistence_id, owner_id, lease_token)
    return result


def _persistence_run_id(
    run_id: str,
    resume_mode: bool,
    state: AgentState,
    execution_attempt: int | None = None,
) -> str:
    if execution_attempt is not None:
        return f"{run_id}-attempt-{max(1, int(execution_attempt))}"
    if not resume_mode:
        return run_id
    return f"{run_id}-resume-{int(state.flags.get('steps') or 0)}"


def _runtime_result(
    state: AgentState,
    original_user_text: str,
    context_id: str | None,
    run_id: str,
    confirmed_filters: dict[str, Any] | None,
    *,
    status: str,
    summary: str | None = None,
    summary_source: str = "agent",
    trace: RunTrace | None = None,
    tool_schemas: list[dict[str, Any]] | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    # Mid-run steering may replace a confirmed filter after run_agent captured
    # its original argument. Always project the latest authoritative context.
    current_confirmed_filters = _effective_confirmed_filters(state, confirmed_filters)
    return {
        "status": status,
        "input": original_user_text,
        "summary": summary if summary is not None else "等待用户补充信息。",
        "summary_source": summary_source,
        "facts": [item.get("output") for item in state.tool_results],
        "tool_results": state.tool_results,
        "final_message": state.final_message,
        "state": state.to_dict(),
        "checkpoint": state.to_checkpoint(),
        "run_logs": list(state.run_logs),
        "trace_id": state.trace_id,
        "trace": trace.to_dict() if trace is not None else {"trace_id": state.trace_id, "events": []},
        "tool_schemas": tool_schemas or [],
        "session_id": session_id,
        "context_id": context_id,
        "run_id": run_id,
        "confirmed_filters": current_confirmed_filters,
        "answer_validation": state.flags.get("answer_validation"),
        "answer_validation_fallback": bool(state.flags.get("answer_validation_fallback")),
        "plan_revision": int(state.flags.get("plan_revision") or 0),
        "input_revision": int(state.flags.get("input_revision") or 0),
        "capabilities": {"midrun_input": MIDRUN_INPUT_ENABLED, "recover": RUN_RECOVERY_ENABLED},
        "runtime": {
            "status": status,
            "plan": state.plan,
            "task_intent": state.task_intent,
            "task_mode": str(state.task_intent.get("mode") or "understanding"),
            "activities": state.run_logs,
            "pending_input": state.pending_input,
            "plan_revision": int(state.flags.get("plan_revision") or 0),
            "input_revision": int(state.flags.get("input_revision") or 0),
            "capabilities": {"midrun_input": MIDRUN_INPUT_ENABLED, "recover": RUN_RECOVERY_ENABLED},
            "last_event_seq": len(trace.events) if trace is not None else 0,
        },
    }
