"""Validation helpers for OpenAI-compatible assistant/tool message sequences."""

from __future__ import annotations

from typing import Any


class MessageProtocolError(ValueError):
    """Raised when a message history cannot safely be sent to a model."""


def validate_message_protocol(
    messages: list[dict[str, Any]], *, allow_pending_tail: bool = False
) -> None:
    """Require every assistant tool call to have exactly one matching tool result.

    The only permitted incomplete suffix is the current pending tool batch when
    ``allow_pending_tail`` is true. Tool messages can never appear without an
    earlier assistant call, and call IDs must be unique within the history.
    """
    outstanding: list[str] = []
    seen: set[str] = set()
    for index, message in enumerate(messages):
        role = message.get("role")
        if outstanding and role != "tool":
            raise MessageProtocolError(
                f"tool_calls 未闭合时消息 {index} 的角色必须为 tool"
            )
        if role == "assistant":
            calls = message.get("tool_calls") or []
            local: list[str] = []
            for call in calls:
                call_id = str(call.get("id") or "").strip()
                if not call_id:
                    raise MessageProtocolError(f"assistant 消息 {index} 有空 tool_call_id")
                if call_id in local or call_id in seen:
                    raise MessageProtocolError(f"重复 tool_call_id：{call_id}")
                local.append(call_id)
                seen.add(call_id)
            outstanding = local
        elif role == "tool":
            call_id = str(message.get("tool_call_id") or "").strip()
            if not call_id or call_id not in outstanding:
                raise MessageProtocolError(f"tool 消息没有对应的 assistant 调用：{call_id}")
            outstanding.remove(call_id)
    if outstanding and not allow_pending_tail:
        raise MessageProtocolError(
            "存在未完成的 tool_calls：" + ", ".join(outstanding)
        )
