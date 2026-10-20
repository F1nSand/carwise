"""Central decision for whether an Agent candidate may become a durable success."""

from __future__ import annotations

from typing import Any

from backend.agent_runtime.run_contracts import RunOutcome, validate_outcome


def plan_ready_for_completion(state: Any) -> bool:
    """A validated answer may close the final running step in the runtime."""
    plan = list(getattr(state, "plan", []) or [])
    if not plan or plan[-1].get("kind") != "finalize" or plan[-1].get("status") != "running":
        return False
    if any(str(step.get("status") or "") not in {"complete", "skipped"} for step in plan[:-1]):
        return False
    if state.flags.get("pending_plan_revision") or state.pending_input is not None:
        return False
    if not state.flags.get("answer_finalized") or not str(state.flags.get("validated_answer") or "").strip():
        return False
    required_goals = {
        str(goal.get("id"))
        for goal in (state.task_intent or {}).get("goals") or []
        if isinstance(goal, dict)
    }
    final_step_id = str(plan[-1].get("id") or "")
    current_revision = int((state.task_context or {}).get("revision") or 0)
    return any(
        result.get("status") == "done"
        and str(result.get("tool_name") or "") == "finalize_answer"
        and str(result.get("plan_step_id") or "") == final_step_id
        and int(result.get("constraint_revision", current_revision)) == current_revision
        and isinstance(result.get("output"), dict)
        and result["output"].get("ok") is True
        and bool((result["output"].get("answer_validation") or {}).get("valid"))
        and required_goals.issubset({str(value) for value in result.get("goal_ids") or []})
        for result in state.tool_results
    )


def decide_completion(
    state: Any,
    candidate: dict[str, Any],
    *,
    execution_status: str,
    report: dict[str, Any] | None,
    projection_error: str | None = None,
    evidence_is_stale: bool = False,
) -> RunOutcome:
    """Turn the loop's final candidate into a validated completed/failed outcome."""
    flags = state.flags
    failure_code: str | None = None
    retryable = False
    message = str(candidate.get("summary") or "").strip()
    validation = flags.get("answer_validation")
    validation_issues = validation.get("issues") if isinstance(validation, dict) else []
    answer_references_stale_evidence = any(
        isinstance(issue, dict) and issue.get("code") == "stale_evidence"
        for issue in validation_issues or []
    )

    if execution_status == "model_unavailable":
        failure_code, retryable = "model_unavailable", True
    elif execution_status == "model_error":
        failure_code, retryable = str(flags.get("failure_code") or "model_error"), True
    elif execution_status in {"error", "invalid_input"}:
        failure_code, retryable = ("invalid_input" if execution_status == "invalid_input" else "agent_error"), False
    elif evidence_is_stale or answer_references_stale_evidence:
        failure_code = "evidence_stale"
    elif flags.get("answer_validation_failed"):
        failure_code = "answer_invalid"
    elif flags.get("max_steps_exceeded") or flags.get("budget_exhausted"):
        failure_code = "budget_exhausted"
    elif state.pending_input:
        failure_code = "protocol_invariant"
    elif not getattr(state, "task_intent", None):
        failure_code = "task_intent_missing"
    elif not state.plan:
        failure_code = "plan_missing"
    elif state.flags.get("pending_plan_revision"):
        failure_code = "plan_revision_incomplete"
    elif (
        not all(str(step.get("status") or "") in {"complete", "skipped"} for step in state.plan)
        and not plan_ready_for_completion(state)
    ):
        failure_code = "plan_incomplete"
    elif state.task_intent.get("needs_plan") and not flags.get("task_completed"):
        failure_code = "task_goals_incomplete"
    elif not message or message == "Agent 未返回最终回答。":
        failure_code = "empty_final_answer"
    elif projection_error:
        failure_code = "projection_failed"
    elif not isinstance(report, dict):
        failure_code = "projection_failed"

    if failure_code:
        public_message = (
            "数据已查询，但最终答案未通过校验。"
            if failure_code == "answer_invalid"
            else message or projection_error or "Agent 未能生成可展示的最终回答。"
        )
        return RunOutcome(
            kind="failed",
            failure_code=failure_code,
            message=public_message,
            retryable=retryable,
        )

    result = {**candidate, "report": report}
    return validate_outcome(RunOutcome(kind="completed", result=result))
