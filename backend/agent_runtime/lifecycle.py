"""Lifecycle boundary that converts durable run snapshots to explicit outcomes."""

from __future__ import annotations

from typing import Any

from backend.agent_runtime.run_contracts import RunOutcome, validate_outcome, validate_snapshot


def outcome_from_snapshot(snapshot: dict[str, Any]) -> RunOutcome | None:
    """Return a validated terminal/waiting outcome, or None for active runs."""
    validate_snapshot(snapshot)
    status = str(snapshot.get("status") or "")
    if status in {"queued", "running"}:
        return None
    if status == "waiting_user":
        return validate_outcome(RunOutcome(
            kind="waiting",
            pending_input=snapshot.get("pending_input"),
            checkpoint=snapshot.get("checkpoint"),
        ))
    if status == "done":
        return validate_outcome(RunOutcome(kind="completed", result=snapshot.get("result")))
    if status == "cancelled":
        return validate_outcome(RunOutcome(
            kind="cancelled",
            message=str(snapshot.get("error") or "运行已取消。"),
        ))
    return validate_outcome(RunOutcome(
        kind="failed",
        result=snapshot.get("result"),
        failure_code=str(snapshot.get("failure_code") or "run_failed"),
        message=str(snapshot.get("error") or "运行失败。"),
        retryable=bool(snapshot.get("retryable")),
    ))
