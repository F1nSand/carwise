"""Typed run lifecycle contracts shared by the API and runtime stores."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, NotRequired, TypedDict

RunStatus = Literal["queued", "running", "waiting_user", "done", "error", "cancelled"]
OutcomeKind = Literal["completed", "waiting", "failed", "cancelled"]


class RunSnapshotV2(TypedDict):
    protocol_version: Literal[2]
    run_id: str
    status: RunStatus
    attempt: int
    state_revision: int
    last_event_seq: int
    pending_input: NotRequired[dict[str, Any] | None]
    result: NotRequired[dict[str, Any] | None]
    error: NotRequired[str | None]
    failure_code: NotRequired[str | None]
    retryable: NotRequired[bool]


@dataclass(frozen=True)
class RunOutcome:
    kind: OutcomeKind
    result: dict[str, Any] | None = None
    pending_input: dict[str, Any] | None = None
    checkpoint: dict[str, Any] | None = None
    failure_code: str | None = None
    message: str | None = None
    retryable: bool = False


class RunContractError(ValueError):
    """Raised when a run snapshot or outcome violates its public contract."""


def validate_transition(previous: str, current: str) -> None:
    allowed: dict[str, set[str]] = {
        "queued": {"queued", "running", "error", "cancelled"},
        "running": {"running", "waiting_user", "done", "error", "cancelled"},
        "waiting_user": {"waiting_user", "queued", "running", "error", "cancelled"},
        "done": {"done"},
        "error": {"error", "queued"},
        "cancelled": {"cancelled"},
    }
    if current not in allowed.get(str(previous), set()):
        raise RunContractError(f"illegal_run_transition:{previous}->{current}")


def validate_pending_input(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not str(value.get("tool_call_id") or "").strip():
        raise RunContractError("waiting_outcome_missing_tool_call_id")
    questions = value.get("questions")
    if not isinstance(questions, list) or not questions:
        raise RunContractError("waiting_outcome_missing_questions")
    if any(not isinstance(item, dict) or not str(item.get("id") or "").strip() for item in questions):
        raise RunContractError("waiting_outcome_invalid_questions")
    return value


def validate_display_result(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RunContractError("completed_outcome_missing_result")
    report = value.get("report")
    final_message = value.get("final_message")
    display_text = value.get("summary")
    if not str(display_text or "").strip() and isinstance(final_message, dict):
        display_text = final_message.get("content")
    if not str(display_text or "").strip() and isinstance(report, dict):
        display_text = report.get("summaryText") or report.get("summaryTitle")
    if not str(display_text or "").strip():
        raise RunContractError("completed_outcome_missing_display_text")
    return value


def validate_outcome(outcome: RunOutcome) -> RunOutcome:
    if outcome.kind == "completed":
        validate_display_result(outcome.result)
        if outcome.pending_input is not None:
            raise RunContractError("completed_outcome_has_pending_input")
    elif outcome.kind == "waiting":
        validate_pending_input(outcome.pending_input)
        if not isinstance(outcome.checkpoint, dict):
            raise RunContractError("waiting_outcome_missing_checkpoint")
    elif outcome.kind == "failed":
        if not str(outcome.failure_code or "").strip():
            raise RunContractError("failed_outcome_missing_failure_code")
    elif outcome.kind == "cancelled":
        if outcome.pending_input is not None:
            raise RunContractError("cancelled_outcome_has_pending_input")
    else:
        raise RunContractError(f"unknown_run_outcome:{outcome.kind}")
    return outcome


def validate_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    protocol_version = snapshot.get("protocol_version")
    if protocol_version is not None and protocol_version != 2:
        raise RunContractError(f"unsupported_run_protocol:{protocol_version}")
    status = str(snapshot.get("status") or "")
    if status not in {"queued", "running", "waiting_user", "done", "error", "cancelled"}:
        raise RunContractError(f"unknown_run_status:{status}")
    for field in ("attempt", "state_revision", "last_event_seq"):
        value = snapshot.get(field)
        if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
            raise RunContractError(f"invalid_run_counter:{field}")
    if status == "waiting_user":
        validate_pending_input(snapshot.get("pending_input"))
        if not isinstance(snapshot.get("checkpoint"), dict):
            raise RunContractError("waiting_snapshot_missing_checkpoint")
    elif status == "done":
        validate_display_result(snapshot.get("result"))
        if snapshot.get("pending_input"):
            raise RunContractError("done_snapshot_has_pending_input")
    elif status == "error":
        if not str(snapshot.get("failure_code") or snapshot.get("error") or "").strip():
            raise RunContractError("error_snapshot_missing_failure")
    elif status == "cancelled" and snapshot.get("pending_input"):
        raise RunContractError("cancelled_snapshot_has_pending_input")
    return snapshot
