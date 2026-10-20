"""Explicit public projections for run, event, and session API responses."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from backend.agent_runtime.runtime_projection import runtime_from_checkpoint


_INTERNAL_KEYS = {
    "reasoning_content",
    "checkpoint",
    "state",
    "trace",
    "trace_id",
    "run_logs",
    "tool_schemas",
    "tool_results",
    "facts",
    "messages",
    "authorization",
    "api_key",
    "x_api_key",
}
_PUBLIC_EVENT_TYPES = {
    "started",
    "task_intent_update",
    "plan_update",
    "input_required",
    "tool_started",
    "tool_completed",
    "tool_call",
    "tool_rejected",
    "message_received",
    "message_applied",
    "recovery_accepted",
    "model_round",
    "final",
    "done",
    "error",
    "cancelled",
}
_PUBLIC_REPORT_KEYS = {
    "id",
    "question",
    "intent",
    "scenarioLabel",
    "generatedAt",
    "dataAsOf",
    "summaryTitle",
    "summaryText",
    "reliability",
    "filtersApplied",
    "methodology",
    "limitations",
    "filters",
    "budgetCount",
    "candidateCount",
    "recommendations",
    "priceChart",
    "sampleChart",
    "series",
    "price",
    "yearChart",
    "cityChart",
    "representativeListings",
    "mileageInsight",
    "answer",
    "answerEvidence",
    "analysisBlocks",
    "evidence",
    "reasonCode",
    "observed",
    "relaxationCandidates",
    "finalPlan",
    "runId",
    "attempt",
}
_SECRET_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _safe_text(value: Any, limit: int = 1000) -> str:
    text = str(value or "")[:limit]
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


def _public_payload(value: Any) -> Any:
    """Copy known report payload values while dropping internal fields defensively."""
    if isinstance(value, Mapping):
        return {
            str(key): _public_payload(item)
            for key, item in value.items()
            if str(key).lower().replace("-", "_") not in _INTERNAL_KEYS
        }
    if isinstance(value, list):
        return [_public_payload(item) for item in value]
    if isinstance(value, tuple):
        return [_public_payload(item) for item in value]
    if isinstance(value, str):
        return _safe_text(value, 20_000)
    return value


def _public_report(value: Any) -> dict[str, Any]:
    """Keep only fields produced by the report projection contract."""
    report = _mapping(value)
    return {
        str(key): _public_payload(item)
        for key, item in report.items()
        if str(key) in _PUBLIC_REPORT_KEYS
    }


def _public_plan(steps: Any) -> list[dict[str, Any]]:
    if not isinstance(steps, list):
        return []
    result: list[dict[str, Any]] = []
    for raw in steps:
        item = _mapping(raw)
        step_id = item.get("id")
        if step_id is None:
            continue
        result.append(
            {
                "id": _safe_text(step_id, 64),
                "title": _safe_text(item.get("title"), 120),
                "description": _safe_text(item.get("description"), 300),
                "tool_names": [_safe_text(name, 80) for name in item.get("tool_names", []) if isinstance(name, str)]
                if isinstance(item.get("tool_names", []), list)
                else [],
                "goal_ids": [_safe_text(goal_id, 64) for goal_id in item.get("goal_ids", []) if isinstance(goal_id, str)]
                if isinstance(item.get("goal_ids", []), list)
                else [],
                "kind": item.get("kind") if item.get("kind") in {"preparation", "clarification", "data", "finalize"} else "data",
                "completion_criteria": item.get("completion_criteria")
                if item.get("completion_criteria") in {"tool_result", "user_answer", "validated_answer"}
                else "tool_result",
                "status": item.get("status") if item.get("status") in {"queued", "running", "complete", "skipped"} else "queued",
                **({"reason": _safe_text(item["reason"], 500)} if item.get("reason") else {}),
            }
        )
    return result


def _public_task_intent(value: Any) -> dict[str, Any] | None:
    intent = _mapping(value)
    goals_raw = intent.get("goals")
    if not isinstance(goals_raw, list):
        return None
    goals = []
    for raw in goals_raw[:8]:
        goal = _mapping(raw)
        if not goal.get("id"):
            continue
        goals.append({
            "id": _safe_text(goal.get("id"), 64),
            "description": _safe_text(goal.get("description"), 300),
            "deliverable": _safe_text(goal.get("deliverable"), 200),
            "depends_on": [_safe_text(value, 64) for value in goal.get("depends_on", []) if isinstance(value, str)]
            if isinstance(goal.get("depends_on", []), list)
            else [],
        })
    if not goals:
        return None
    needs_plan = bool(intent.get("needs_plan"))
    revision = intent.get("revision")
    return {
        "mode": "planned" if needs_plan else "simple",
        "needs_plan": needs_plan,
        "revision": max(1, revision) if isinstance(revision, int) and not isinstance(revision, bool) else 1,
        "goals": goals,
    }


def _public_questions(questions: Any) -> list[dict[str, Any]]:
    if not isinstance(questions, list):
        return []
    result: list[dict[str, Any]] = []
    for raw in questions:
        question = _mapping(raw)
        question_id = question.get("id")
        if question_id is None:
            continue
        options = []
        for raw_option in question.get("options", []) if isinstance(question.get("options", []), list) else []:
            option = _mapping(raw_option)
            if option.get("id") is None:
                continue
            options.append(
                {
                    "id": _safe_text(option.get("id"), 64),
                    "label": _safe_text(option.get("label"), 120),
                    "value": _safe_text(option.get("value"), 500),
                }
            )
        selection_mode = question.get("selection_mode")
        result.append(
            {
                "id": _safe_text(question_id, 64),
                "prompt": _safe_text(question.get("prompt"), 240),
                "options": options,
                "allow_custom": bool(question.get("allow_custom", False)),
                "required": bool(question.get("required", True)),
                "selection_mode": selection_mode if selection_mode in {"single", "multiple"} else "single",
                "max_selections": question.get("max_selections")
                if isinstance(question.get("max_selections"), int) and not isinstance(question.get("max_selections"), bool)
                else 1,
            }
        )
    return result


def _public_pending_input(value: Any) -> dict[str, Any] | None:
    pending = _mapping(value)
    if not pending:
        return None
    result: dict[str, Any] = {"questions": _public_questions(pending.get("questions"))}
    for key in ("tool_call_id", "plan_step_id"):
        if pending.get(key) is not None:
            result[key] = _safe_text(pending[key], 128)
    for key in ("step", "resume_revision"):
        item = pending.get(key)
        if isinstance(item, int) and not isinstance(item, bool):
            result[key] = item
    prefilled = pending.get("prefilled_answers")
    if isinstance(prefilled, list):
        result["prefilled_answers"] = _public_payload(prefilled)
    return result


def _public_event_output(event_type: str, output: Any) -> dict[str, Any]:
    value = _mapping(output)
    if event_type == "task_intent_update":
        intent = _public_task_intent(value.get("task_intent"))
        return {"task_intent": intent} if intent else {}
    if event_type == "plan_update":
        steps = value.get("steps")
        if steps is None:
            return {}
        projected = {"steps": _public_plan(steps)}
        if isinstance(value.get("revision"), int) and not isinstance(value["revision"], bool):
            projected["revision"] = max(0, value["revision"])
        if value.get("reason"):
            projected["reason"] = _safe_text(value["reason"], 500)
        return projected
    if event_type == "input_required":
        questions = _public_questions(value.get("questions"))
        return {"questions": questions} if questions else {}
    if event_type in {"tool_completed", "tool_rejected"}:
        result: dict[str, Any] = {}
        if isinstance(value.get("ok"), bool):
            result["ok"] = value["ok"]
        for key in ("note", "error", "error_code"):
            if value.get(key) is not None:
                result[key] = _safe_text(value[key], 500)
        return result
    if event_type == "final":
        content = _mapping(value).get("content")
        if content is not None:
            return {"content": _safe_text(content, 20_000)}
        return {}
    if event_type == "done":
        return {}
    return {}


def public_terminal_result(result: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Return the compact, user-facing terminal result used by SSE and snapshots."""
    if not isinstance(result, Mapping):
        return None
    compact: dict[str, Any] = {}
    for key in ("status", "summary_source", "run_id", "context_id"):
        if result.get(key) is not None:
            compact[key] = _safe_text(result[key], 256)
    if result.get("conversation_id") is not None:
        compact["conversation_id"] = _safe_text(result["conversation_id"], 256)
    report_ref = _mapping(result.get("report_ref"))
    if report_ref:
        compact["report_ref"] = {
            key: _safe_text(report_ref[key], 256)
            for key in ("report_id", "origin_run_id", "parent_report_id", "conversation_id")
            if report_ref.get(key) is not None
        }
        if isinstance(report_ref.get("version"), int) and not isinstance(report_ref["version"], bool):
            compact["report_ref"]["version"] = max(1, int(report_ref["version"]))
    entry_ids = _mapping(result.get("entry_ids"))
    if entry_ids:
        compact["entry_ids"] = {
            key: _safe_text(entry_ids[key], 128)
            for key in ("assistant_entry_id", "report_entry_id")
            if entry_ids.get(key) is not None
        }
    if result.get("summary") is not None:
        compact["summary"] = _safe_text(result["summary"], 20_000)
    if isinstance(result.get("report"), Mapping):
        report = _public_report(result["report"])
        state = _mapping(result.get("state"))
        runtime = _mapping(result.get("runtime"))
        state_plan = state.get("plan") or runtime.get("plan")
        if isinstance(state_plan, list) and state_plan:
            report["finalPlan"] = _public_plan(state_plan)
        if result.get("run_id") is not None:
            report["runId"] = _safe_text(result["run_id"], 128)
        if isinstance(result.get("attempt"), int) and not isinstance(result.get("attempt"), bool):
            report["attempt"] = max(0, result["attempt"])
        compact["report"] = report
    if isinstance(result.get("confirmed_filters"), Mapping):
        compact["confirmed_filters"] = _public_payload(result["confirmed_filters"])
    final_message = _mapping(result.get("final_message"))
    if final_message:
        public_message = {
            key: _safe_text(final_message[key], 20_000)
            for key in ("role", "content", "source")
            if final_message.get(key) is not None
        }
        if public_message:
            compact["final_message"] = public_message
    return compact


def _public_activity(raw: Any) -> dict[str, Any] | None:
    value = _mapping(raw)
    event_type = str(value.get("event_type") or "")
    projected = public_run_event(value)
    if event_type not in {"tool_started", "tool_completed", "tool_rejected", "tool_call", "input_required"}:
        return None
    return projected


def public_run_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Project a stored run to the fields consumed by the browser UI."""
    result: dict[str, Any] = {"protocol_version": 3}
    runtime_raw = _mapping(snapshot.get("runtime"))
    checkpoint = _mapping(snapshot.get("checkpoint"))
    if checkpoint and ("plan" in checkpoint or "task_intent" in checkpoint):
        runtime_raw = runtime_from_checkpoint(
            checkpoint,
            status=str(snapshot.get("status") or runtime_raw.get("status") or "running"),
            state_revision=snapshot.get("state_revision") if isinstance(snapshot.get("state_revision"), int) else None,
            capabilities=_mapping(runtime_raw.get("capabilities")),
        )
    runtime_state = _mapping(runtime_raw.get("state"))
    for key in (
        "run_id", "session_id", "context_id", "status", "created_at", "updated_at", "resume_revision",
        "queue_position", "failure_code",
    ):
        value = snapshot.get(key)
        if value is not None:
            result[key] = value if isinstance(value, int) else _safe_text(value, 256)
    attempt = snapshot.get("attempt")
    result["attempt"] = max(0, int(attempt)) if isinstance(attempt, int) and not isinstance(attempt, bool) else 0
    # Storage keeps waiting_user until a worker claims an accepted answer.
    # Public consumers must see a queued continuation, not an actionable wait.
    if result.get("status") == "waiting_user" and _mapping(snapshot.get("request")).get("resume_ready"):
        result["status"] = "queued"
    state_revision = snapshot.get("state_revision")
    if not isinstance(state_revision, int) or isinstance(state_revision, bool):
        state_revision = snapshot.get("version")
    result["state_revision"] = max(0, int(state_revision)) if isinstance(state_revision, int) and not isinstance(state_revision, bool) else 0
    if isinstance(snapshot.get("next_seq"), int) and not isinstance(snapshot.get("next_seq"), bool):
        result["last_event_seq"] = max(0, int(snapshot["next_seq"]) - 1)
    elif isinstance(runtime_raw.get("last_event_seq"), int) and not isinstance(runtime_raw.get("last_event_seq"), bool):
        result["last_event_seq"] = max(0, runtime_raw["last_event_seq"])
    elif isinstance(runtime_state.get("last_event_seq"), int) and not isinstance(runtime_state.get("last_event_seq"), bool):
        result["last_event_seq"] = max(0, runtime_state["last_event_seq"])
    covered_event_seq = snapshot.get("covered_event_seq")
    if isinstance(covered_event_seq, int) and not isinstance(covered_event_seq, bool):
        result["covered_event_seq"] = max(0, covered_event_seq)
    recoverable = snapshot.get("recoverable")
    if isinstance(recoverable, bool):
        result["recoverable"] = recoverable
    retryable = snapshot.get("retryable")
    if isinstance(retryable, bool):
        result["retryable"] = retryable
    input_revision = snapshot.get("input_revision")
    if input_revision is None:
        input_revision = runtime_raw.get("input_revision")
    if input_revision is None:
        input_revision = runtime_state.get("input_revision")
    if isinstance(input_revision, int) and not isinstance(input_revision, bool):
        result["input_revision"] = max(0, input_revision)
    if snapshot.get("recovery_reason"):
        result["recovery_reason"] = _safe_text(snapshot["recovery_reason"], 500)
    capabilities = _mapping(snapshot.get("capabilities") or runtime_raw.get("capabilities") or runtime_state.get("capabilities"))
    if capabilities:
        result["capabilities"] = {
            "midrun_input": bool(capabilities.get("midrun_input")),
            "recover": bool(capabilities.get("recover")),
        }
    plan_revision = snapshot.get("plan_revision")
    if plan_revision is None:
        plan_revision = runtime_raw.get("plan_revision")
    if plan_revision is None:
        plan_revision = runtime_state.get("plan_revision")
    if isinstance(plan_revision, int) and not isinstance(plan_revision, bool):
        result["plan_revision"] = max(0, plan_revision)
    task_revision = snapshot.get("task_revision")
    if task_revision is None:
        task_revision = runtime_raw.get("task_revision")
    if task_revision is None:
        task_intent = _mapping(runtime_raw.get("task_intent") or runtime_state.get("task_intent"))
        task_revision = task_intent.get("revision")
    if isinstance(task_revision, int) and not isinstance(task_revision, bool):
        result["task_revision"] = max(0, task_revision)
    if snapshot.get("input") is not None:
        result["input"] = _safe_text(snapshot["input"], 20_000)
    if snapshot.get("error") is not None:
        result["error"] = _safe_text(snapshot["error"], 1000)
    if isinstance(snapshot.get("retryable"), bool):
        result["retryable"] = snapshot["retryable"]
    pending = _public_pending_input(snapshot.get("pending_input"))
    if pending is None:
        pending = _public_pending_input(runtime_raw.get("pending_input"))
    if pending is None:
        pending = _public_pending_input(runtime_state.get("pending_input"))
    if pending is None:
        checkpoint = _mapping(snapshot.get("checkpoint"))
        pending = _public_pending_input(checkpoint.get("pending_input"))
    if result.get("status") != "waiting_user":
        pending = None
    if pending is not None:
        # The run counter is the CAS barrier; the Agent's nested counter counts
        # questions and may differ, including in already persisted old waits.
        resume_revision = snapshot.get("resume_revision")
        if isinstance(resume_revision, int) and not isinstance(resume_revision, bool):
            pending["resume_revision"] = max(0, resume_revision)
        result["pending_input"] = pending

    stored_result = _mapping(snapshot.get("result"))
    state = _mapping(runtime_raw.get("state")) if isinstance(runtime_raw.get("state"), Mapping) else runtime_raw
    if not state:
        state = _mapping(stored_result.get("state"))
    status = result.get("status") or runtime_raw.get("status") or state.get("runtime_status")
    plan = runtime_raw.get("plan") or state.get("plan")
    activities_raw = runtime_raw.get("activities") or state.get("run_logs")
    activities = []
    if isinstance(activities_raw, list):
        activities = [item for raw in activities_raw if (item := _public_activity(raw)) is not None]
    public_runtime = {
        "status": _safe_text(status, 64),
        "plan": _public_plan(plan),
        "activities": activities,
    }
    task_intent = _public_task_intent(runtime_raw.get("task_intent") or state.get("task_intent"))
    if task_intent:
        public_runtime["task_intent"] = task_intent
        public_runtime["task_mode"] = task_intent["mode"]
    else:
        public_runtime["task_mode"] = "understanding"
    plan_revision = result.get("plan_revision")
    if isinstance(plan_revision, int) and not isinstance(plan_revision, bool):
        public_runtime["plan_revision"] = max(0, plan_revision)
    if "last_event_seq" in result:
        public_runtime["last_event_seq"] = result["last_event_seq"]
    if "covered_event_seq" in result:
        public_runtime["covered_event_seq"] = result["covered_event_seq"]
    if "state_revision" in result:
        public_runtime["state_revision"] = result["state_revision"]
    if "attempt" in result:
        public_runtime["attempt"] = result["attempt"]
    if "task_revision" in result:
        public_runtime["task_revision"] = result["task_revision"]
    current_stage = runtime_raw.get("current_stage")
    if isinstance(current_stage, Mapping):
        public_runtime["current_stage"] = {
            "id": _safe_text(current_stage.get("id"), 64),
            "title": _safe_text(current_stage.get("title"), 120),
        }
    if capabilities:
        public_runtime["capabilities"] = result["capabilities"]
    if "input_revision" in result:
        public_runtime["input_revision"] = result["input_revision"]
    if pending is not None:
        public_runtime["pending_input"] = pending
    result["runtime"] = public_runtime
    public_result = public_terminal_result(stored_result)
    if public_result is not None:
        result["result"] = public_result
    return result


def public_run_event(event: Mapping[str, Any]) -> dict[str, Any]:
    """Project one event according to its event type; unknown fields are dropped."""
    requested_type = str(event.get("event_type") or "message")
    event_type = requested_type if requested_type in _PUBLIC_EVENT_TYPES else "message"
    result: dict[str, Any] = {}
    if event.get("run_id") is not None:
        result["run_id"] = _safe_text(event["run_id"], 128)
    for key in ("attempt", "state_revision", "event_seq"):
        value = event.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            result[key] = max(0, value)
    if isinstance(event.get("seq"), int):
        result["seq"] = event["seq"]
    result["event_type"] = event_type

    for key in ("step", "duration_ms"):
        value = event.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            result[key] = max(0, value)
    if event_type == "started":
        for key in ("queue_wait_ms", "attempt"):
            value = event.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                result[key] = max(0, value)
    if event.get("status") is not None:
        result["status"] = _safe_text(event["status"], 64)

    source = _mapping(event.get("input"))
    if event_type in {"tool_started", "tool_completed", "tool_call", "tool_rejected", "input_required"}:
        public_input = {
            key: _safe_text(source[key], 128)
            for key in ("tool_call_id", "tool_name", "plan_step_id")
            if source.get(key) is not None
        }
        if public_input:
            result["input"] = public_input
        if event_type == "input_required":
            result["output"] = _public_event_output(event_type, event.get("output"))
            pending = _public_pending_input(event.get("pending_input"))
            if pending is not None:
                result["pending_input"] = pending
            if event.get("resume_revision") is not None:
                result["resume_revision"] = event["resume_revision"]
                if pending is not None:
                    pending["resume_revision"] = event["resume_revision"]
        elif event_type in {"tool_completed", "tool_rejected"}:
            result["output"] = _public_event_output(event_type, event.get("output"))
        return result

    if event_type == "plan_update":
        result["output"] = _public_event_output(event_type, event.get("output"))
    elif event_type == "task_intent_update":
        result["output"] = _public_event_output(event_type, event.get("output"))
    elif event_type in {"message_received", "message_applied"}:
        output = _mapping(event.get("output"))
        source_message = _mapping(event.get("input"))
        identity = output.get("client_message_id") or source_message.get("client_message_id") or event.get("client_message_id")
        result["output"] = {"client_message_id": _safe_text(identity, 128)}
        revision = output.get("revision", event.get("revision"))
        if isinstance(revision, int) and not isinstance(revision, bool):
            result["output"]["revision"] = max(0, revision)
    elif event_type == "model_round":
        pass
    elif event_type == "recovery_accepted":
        result["status"] = _safe_text(event.get("status") or "queued", 64)
    elif event_type == "cancelled":
        result["error"] = _safe_text(event.get("error"), 1000)
    elif event_type == "final":
        result["output"] = _public_event_output(event_type, event.get("output"))
    elif event_type == "done":
        public_result = public_terminal_result(_mapping(event.get("result")))
        if public_result is not None:
            result["result"] = public_result
        direct_report_ref = public_terminal_result({"report_ref": event.get("report_ref")})
        if direct_report_ref and direct_report_ref.get("report_ref"):
            result["report_ref"] = direct_report_ref["report_ref"]
        direct_entry_ids = public_terminal_result({"entry_ids": event.get("entry_ids")})
        if direct_entry_ids and direct_entry_ids.get("entry_ids"):
            result["entry_ids"] = direct_entry_ids["entry_ids"]
    elif event_type == "error":
        result["error"] = _safe_text(event.get("error"), 1000)
        if event.get("failure_code") is not None:
            result["failure_code"] = _safe_text(event.get("failure_code"), 80)
        if isinstance(event.get("retryable"), bool):
            result["retryable"] = event["retryable"]
    return result


def public_session_message(record: Mapping[str, Any]) -> dict[str, Any]:
    """Expose conversational text only; hide model internals and tool payloads."""
    role = str(record.get("role") or "")
    if role not in {"user", "assistant", "tool"}:
        return {}
    if role == "assistant" and record.get("tool_calls"):
        return {}
    if role == "tool":
        content = "工具结果已处理。"
    else:
        content = _safe_text(record.get("content"), 20_000)
    return {"role": role, "content": content}
