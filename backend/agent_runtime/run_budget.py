"""Serializable logical-run budgets shared by pause/resume segments."""

from __future__ import annotations

from typing import Any


DEFAULT_LIMITS = {
    "model_calls": 12,
    "tool_executions": 24,
    # Two bounded semantic/schema repairs after the original finalize attempt.
    "answer_repairs": 2,
    "replans": 2,
    "active_execution_seconds": 180.0,
    "truncation_retries": 1,
}


def new_run_budget(limits: dict[str, Any] | None = None) -> dict[str, Any]:
    configured = {**DEFAULT_LIMITS, **(limits or {})}
    return {
        "limits": configured,
        "model_calls": 0,
        "tool_executions": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "active_execution_seconds": 0.0,
        "answer_repairs": 0,
        "replans": 0,
        "truncation_retries": 0,
        "usage_source": "provider",
        "estimated_usage_calls": 0,
    }


def reserve(budget: dict[str, Any], name: str, amount: int | float = 1) -> bool:
    """Reserve before starting work so failures and resumes cannot refresh quotas."""
    limits = budget.setdefault("limits", DEFAULT_LIMITS.copy())
    used = float(budget.get(name) or 0)
    limit = float(limits.get(name, float("inf")))
    if used + amount > limit:
        return False
    budget[name] = used + amount
    return True


def settle_model_usage(
    budget: dict[str, Any], usage: dict[str, Any], *, estimated_prompt_tokens: int, answer_text: str = ""
) -> None:
    if not usage or not isinstance(usage.get("total_tokens"), (int, float)):
        prompt = max(0, int(estimated_prompt_tokens))
        completion = max(1, (len(str(answer_text)) + 3) // 4)
        budget["prompt_tokens"] = int(budget.get("prompt_tokens") or 0) + prompt
        budget["completion_tokens"] = int(budget.get("completion_tokens") or 0) + completion
        budget["total_tokens"] = int(budget.get("total_tokens") or 0) + prompt + completion
        budget["estimated_usage_calls"] = int(budget.get("estimated_usage_calls") or 0) + 1
        budget["usage_source"] = "estimated"
        return
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = usage.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            budget[key] = int(budget.get(key) or 0) + max(0, int(value))


def add_active_seconds(budget: dict[str, Any], seconds: float) -> None:
    budget["active_execution_seconds"] = max(0.0, float(budget.get("active_execution_seconds") or 0)) + max(0.0, float(seconds))
