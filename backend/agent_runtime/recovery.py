"""Deterministic failure deduplication and bounded retry classification."""

from __future__ import annotations

import hashlib
import json
from typing import Any

_ALIASES = {"pure electric": "纯电动", "ev": "纯电动", "phev": "插电式混合动力", "hev": "混合动力"}


def normalize_arguments(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): normalize_arguments(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, list):
        return [normalize_arguments(item) for item in value]
    if isinstance(value, str):
        text = value.strip()
        return _ALIASES.get(text.casefold(), text)
    if isinstance(value, float):
        return 0 if value == 0 else float(format(value, ".12g"))
    return value


def tool_call_fingerprint(tool_name: str, arguments: dict[str, Any], *, dataset_version: str = "unknown", constraint_revision: str | int | None = 0) -> str:
    payload = json.dumps({"tool": str(tool_name), "arguments": normalize_arguments(arguments),
                          "dataset_version": str(dataset_version), "constraint_revision": constraint_revision},
                         ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def is_deterministic_failure(output: dict[str, Any]) -> bool:
    status = str(output.get("status") or output.get("result_status") or "").lower()
    code = str(output.get("error_code") or output.get("reason_code") or "").lower()
    return status in {"empty", "unsupported", "invalid_arguments"} or code in {
        "unsupported_filter", "invalid_arguments", "validation_error"
    } or (output.get("ok") is False and not output.get("retryable"))
