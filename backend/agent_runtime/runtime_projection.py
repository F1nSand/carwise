"""Small, checkpoint-derived runtime view shared by repository and API layers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

RUNTIME_PROTOCOL_VERSION = 3


def _int(value: Any, default: int = 0) -> int:
    return max(0, value) if isinstance(value, int) and not isinstance(value, bool) else default


def runtime_from_checkpoint(
    checkpoint: Mapping[str, Any],
    *,
    status: str,
    state_revision: int | None = None,
    capabilities: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a bounded runtime projection without copying messages or tool results."""
    flags = checkpoint.get("flags") if isinstance(checkpoint.get("flags"), Mapping) else {}
    plan = checkpoint.get("plan") if isinstance(checkpoint.get("plan"), list) else []
    activities = checkpoint.get("run_logs") if isinstance(checkpoint.get("run_logs"), list) else []
    task_intent = checkpoint.get("task_intent") if isinstance(checkpoint.get("task_intent"), Mapping) else None
    task_revision = _int((task_intent or {}).get("revision"), _int(flags.get("task_intent_revision")))
    plan_revision = _int(flags.get("plan_revision"))
    current = next((step for step in plan if isinstance(step, Mapping) and step.get("status") == "running"), None)
    pending = checkpoint.get("pending_input") if isinstance(checkpoint.get("pending_input"), Mapping) else None

    projection: dict[str, Any] = {
        "status": str(status or checkpoint.get("runtime_status") or "running"),
        "plan": plan,
        "activities": activities,
        "task_mode": str((task_intent or {}).get("mode") or ("planned" if task_intent else "understanding")),
        "plan_revision": plan_revision,
        "task_revision": task_revision,
        "current_stage": (
            {"id": str(current.get("id") or ""), "title": str(current.get("title") or "")}
            if current is not None
            else None
        ),
        "input_revision": _int(flags.get("input_revision")),
    }
    if task_intent is not None:
        projection["task_intent"] = dict(task_intent)
    if pending is not None:
        projection["pending_input"] = dict(pending)
    if isinstance(state_revision, int) and not isinstance(state_revision, bool):
        projection["state_revision"] = max(0, state_revision)
    if capabilities:
        projection["capabilities"] = {
            "midrun_input": bool(capabilities.get("midrun_input")),
            "recover": bool(capabilities.get("recover")),
        }
    return projection
