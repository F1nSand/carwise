"""Compatibility layer for normalized tool results."""

from __future__ import annotations

from typing import Any

RESULT_STATUSES = {
    "success", "empty", "invalid_arguments", "unsupported", "transient_error",
    "cancelled", "timeout", "unknown_outcome", "error",
}


def normalize_tool_result(tool_name: str, value: Any) -> dict[str, Any]:
    """Adapt existing business-tool dictionaries to the additive ToolResult shape."""
    if not isinstance(value, dict):
        value = {"ok": False, "error": "工具必须返回 JSON 对象"}
    output = dict(value)
    legacy_status = str(output.get("result_status") or "").lower()
    error_code = str(output.get("error_code") or output.get("reason_code") or "")
    if output.get("ok") is True:
        status = "empty" if legacy_status == "empty" else "success"
    elif error_code in {"unsupported_filter", "unsupported"}:
        status = "unsupported"
    elif error_code in {"invalid_arguments", "validation_error"}:
        status = "invalid_arguments"
    elif error_code in {"timeout", "deadline_exceeded"}:
        status = "timeout"
    elif error_code in {"cancelled", "canceled"}:
        status = "cancelled"
    elif error_code in {"unknown_outcome", "unknown_outcome_non_idempotent"}:
        status = "unknown_outcome"
    elif output.get("retryable"):
        status = "transient_error"
    elif legacy_status == "empty":
        status = "empty"
    else:
        status = "error"
    data = output.get("data")
    if data is None:
        data = {key: item for key, item in output.items() if key not in {
            "schema_version", "status", "error_code", "retryable", "evidence", "data"
        }}
    output.update({
        "schema_version": int(output.get("schema_version") or 1),
        "status": status,
        "error_code": error_code or None,
        "retryable": bool(output.get("retryable", status == "transient_error")),
        "data": data,
        "evidence": list(output.get("evidence") or []),
        "tool": str(output.get("tool") or tool_name),
        "ok": status in {"success", "empty"},
    })
    return output
