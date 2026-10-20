"""Normalize provider responses at the boundary before they enter the Agent loop."""

from __future__ import annotations

from dataclasses import dataclass
from time import perf_counter
from typing import Any


def _get(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


@dataclass(frozen=True)
class ModelTurn:
    response: Any
    message: Any
    category: str
    finish_reason: str | None
    provider_request_id: str | None
    latency_ms: int


class ModelTurnRejected(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def inspect_response(response: Any, *, latency_ms: int = 0) -> ModelTurn:
    """Classify a non-streaming chat response without discarding provider metadata."""
    choices = _get(response, "choices", []) or []
    if not choices:
        raise ValueError("模型响应缺少 choices")
    choice = choices[0]
    message = _get(choice, "message", {}) or {}
    raw_reason = _get(choice, "finish_reason")
    finish_reason = str(raw_reason) if raw_reason is not None else None
    content = str(_get(message, "content", "") or "").strip()
    tool_calls = _get(message, "tool_calls", []) or []
    refusal = _get(message, "refusal")

    # A length finish takes precedence over the apparent shape: tool arguments
    # may have been cut off even when the provider included a tool_calls array.
    if finish_reason == "length":
        category = "truncated"
    elif finish_reason == "content_filter" or refusal:
        category = "refusal"
    elif tool_calls:
        category = "tool_calls"
    elif content:
        category = "text"
    else:
        category = "empty"

    request_id = _get(response, "id")
    return ModelTurn(
        response=response,
        message=message,
        category=category,
        finish_reason=finish_reason,
        provider_request_id=str(request_id) if request_id else None,
        latency_ms=max(0, int(latency_ms)),
    )


def complete_turn(client: Any, **request: Any) -> ModelTurn:
    """Make one provider call and return a classified, timed turn."""
    started = perf_counter()
    response = client.complete(**request)
    latency_ms = int((perf_counter() - started) * 1000)
    return inspect_response(response, latency_ms=latency_ms)


def failure_code(error: BaseException) -> str:
    """Map common transport timeout classes while preserving generic provider errors."""
    name = type(error).__name__.casefold()
    if isinstance(error, TimeoutError) or "timeout" in name or "timedout" in name:
        return "model_timeout"
    if name in {"apiconnectionerror", "connecterror", "connectionerror"}:
        return "model_unavailable"
    return "model_error"
