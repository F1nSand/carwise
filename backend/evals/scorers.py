from __future__ import annotations

import json
import re
from typing import Any

from .models import EvalCase, EvalResult


def _tool_calls(run: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    direct = run.get("tool_calls")
    if isinstance(direct, list):
        candidates.extend(item for item in direct if isinstance(item, dict))
    trace = run.get("trace")
    events = trace.get("events", []) if isinstance(trace, dict) else []
    for event in events if isinstance(events, list) else []:
        if not isinstance(event, dict) or event.get("event_type") not in {"tool_call", "tool_started", "tool_completed", "tool_rejected", "tool_cancelled"}:
            continue
        payload = event.get("input") if isinstance(event.get("input"), dict) else {}
        candidates.append({**payload, **event, "input": payload.get("arguments", payload.get("input")), "tool": payload.get("tool_name", event.get("tool_name"))})
    # Keep one logical call per tool_call_id; prefer completed/result records over start events.
    by_id: dict[str, dict[str, Any]] = {}
    anonymous: list[dict[str, Any]] = []
    priority = {"tool_started": 0, "tool_call": 1, "tool_rejected": 2, "tool_cancelled": 2, "tool_completed": 3}
    for item in candidates:
        call_id = str(item.get("tool_call_id") or item.get("id") or "")
        if not call_id:
            anonymous.append(item)
            continue
        old = by_id.get(call_id)
        if old is None:
            by_id[call_id] = item
            continue
        if priority.get(str(item.get("event_type") or "tool_completed"), 3) >= priority.get(str(old.get("event_type") or "tool_completed"), 3):
            by_id[call_id] = {**old, **item, "arguments": item.get("arguments", old.get("arguments")), "input": item.get("input", old.get("input"))}
    results = run.get("tool_results")
    if isinstance(results, list):
        for item in results:
            if not isinstance(item, dict):
                continue
            call_id = str(item.get("tool_call_id") or item.get("id") or "")
            if call_id and call_id in by_id:
                by_id[call_id] = {**by_id[call_id], **item}
            else:
                anonymous.append(item)
    return [*by_id.values(), *anonymous]


def _tool_name(call: dict[str, Any]) -> str:
    function = call.get("function") if isinstance(call.get("function"), dict) else {}
    return str(call.get("tool") or call.get("tool_name") or function.get("name") or "")


def _arguments(call: dict[str, Any]) -> dict[str, Any]:
    value = call.get("arguments") or call.get("input") or (call.get("function") or {}).get("arguments")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return {}
    return value if isinstance(value, dict) else {}


def extract_tool_calls(run: dict[str, Any]) -> list[dict[str, Any]]:
    """Public stable call extractor used by trajectory metrics and scorers."""
    return _tool_calls(run)


def _path(value: Any, path: str) -> Any:
    for part in path.split(".") if path else []:
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def score_run(case: EvalCase, run: dict[str, Any]) -> EvalResult:
    if not isinstance(run, dict):
        raise ValueError("run 必须是对象")
    expect = case.expect
    violations: list[str] = []
    calls = _tool_calls(run)
    names = [_tool_name(call) for call in calls]
    expected_status = expect.get("status", "done")
    status_ok = str(run.get("status") or "") == str(expected_status)
    if not status_ok:
        violations.append(f"status: expected {expected_status!r}, got {run.get('status')!r}")
    required = expect.get("required_tools", [])
    for tool in required:
        if str(tool) not in names:
            violations.append(f"required_tools: missing {tool}")
    forbidden = expect.get("forbidden_tools", [])
    for tool in forbidden:
        if str(tool) in names:
            violations.append(f"forbidden_tools: found {tool}")
    pending_expect = expect.get("pending_decision_reason")
    if pending_expect:
        pending = (run.get("state", {}).get("flags", {}) or {}).get("pending_user_decision", {})
        if not isinstance(pending, dict) or pending.get("reason_code") != pending_expect:
            violations.append(f"pending_decision_reason: expected {pending_expect}")
        checkpoint = run.get("checkpoint")
        if not isinstance(checkpoint, dict) or not checkpoint.get("pending_input"):
            violations.append("pending_decision_reason: waiting checkpoint must retain an actionable question")
    for matcher in expect.get("tool_arguments", []):
        if not isinstance(matcher, dict):
            violations.append("tool_arguments: matcher must be object")
            continue
        matches = [call for call in calls if _tool_name(call) == str(matcher.get("tool"))]
        occurrence = matcher.get("occurrence", 0)
        if not isinstance(occurrence, int) or occurrence < 0:
            violations.append("tool_arguments: occurrence must be a nonnegative integer")
            continue
        actual = _path(_arguments(matches[occurrence]), str(matcher.get("path") or "")) if len(matches) > occurrence else None
        if "equals" in matcher and actual != matcher["equals"]:
            violations.append(f"tool_arguments.{matcher.get('tool')}.{matcher.get('path')}: expected equals")
        if "min" in matcher and (not isinstance(actual, (int, float)) or actual < matcher["min"]):
            violations.append(f"tool_arguments.{matcher.get('tool')}.{matcher.get('path')}: below min")
        if "max" in matcher and (not isinstance(actual, (int, float)) or actual > matcher["max"]):
            violations.append(f"tool_arguments.{matcher.get('tool')}.{matcher.get('path')}: above max")
    if expect.get("plan_complete") is True:
        plan = run.get("plan") or run.get("state", {}).get("plan") if isinstance(run.get("state"), dict) else run.get("plan")
        if not isinstance(plan, list) or not plan or any(item.get("status") not in {"complete", "skipped"} for item in plan if isinstance(item, dict)):
            violations.append("plan_complete: plan is incomplete")
    plan = run.get("plan") or run.get("state", {}).get("plan") if isinstance(run.get("state"), dict) else run.get("plan")
    plan_titles = [str(item.get("title") or "") for item in plan if isinstance(item, dict)] if isinstance(plan, list) else []
    for title in expect.get("plan_titles", []):
        if str(title) not in plan_titles:
            violations.append(f"plan_titles: missing {title}")
    plan_requirements = expect.get("plan_requirements")
    if isinstance(plan_requirements, dict):
        if not isinstance(plan, list):
            violations.append("plan_requirements: missing plan")
        else:
            min_steps = plan_requirements.get("min_steps", 0)
            if isinstance(min_steps, int) and len(plan) < min_steps:
                violations.append(f"plan_requirements: expected at least {min_steps} steps")
            present_kinds = {
                str(item.get("kind") or "") for item in plan if isinstance(item, dict)
            }
            for kind in plan_requirements.get("required_kinds", []):
                if str(kind) not in present_kinds:
                    violations.append(f"plan_requirements: missing step kind {kind}")
            if plan_requirements.get("titles_nonempty") is True and any(
                not str(item.get("title") or "").strip()
                for item in plan if isinstance(item, dict)
            ):
                violations.append("plan_requirements: every step needs a visible title")
    if expect.get("reason_codes"):
        actual_codes = set(run.get("reason_codes") or [])
        for code in expect["reason_codes"]:
            if code not in actual_codes:
                violations.append(f"reason_codes: missing {code}")
    final_message = run.get("final_message") if isinstance(run.get("final_message"), dict) else {}
    state = run.get("state") if isinstance(run.get("state"), dict) else {}
    flags = state.get("flags") if isinstance(state.get("flags"), dict) else {}
    answer_validation = flags.get("answer_validation") if isinstance(flags.get("answer_validation"), dict) else {}
    report = run.get("report") if isinstance(run.get("report"), dict) else {}
    # Assertions must inspect what the application actually delivers. Raw
    # model text can differ from the validated, server-rendered answer.
    final_text = str(
        run.get("summary")
        or report.get("answer")
        or answer_validation.get("answer")
        or final_message.get("content")
        or run.get("final_text")
        or ""
    )
    for fragment in expect.get("summary_contains", []):
        if str(fragment) not in final_text:
            violations.append(f"summary_contains: missing {fragment}")
    for entity in expect.get("answer_contains_entities", []):
        if str(entity) not in final_text:
            violations.append(f"answer_contains_entities: missing {entity}")
    if expect.get("model_error") is True and not any(marker in final_text for marker in ("模型调用失败", "model_error", "injected provider failure")):
        violations.append("model_error: injected provider failure was not surfaced")
    if expect.get("numeric_grounding"):
        source_parts = [str(turn.content) for turn in case.turns]
        source_parts.extend(json.dumps(item, ensure_ascii=False) for item in (run.get("tool_results") or []))
        source_parts.extend(json.dumps(item, ensure_ascii=False) for item in (run.get("conversions") or []))
        source_text = " ".join(source_parts)
        for number in re.findall(r"(?<![A-Za-z])\d+(?:\.\d+)?", final_text):
            if number not in source_text:
                violations.append(f"numeric_grounding: final_text number {number!r} has no source")
    if expect.get("out_of_domain") and not any(marker in final_text for marker in ("范围", "只能", "不支持")):
        violations.append("out_of_domain: response lacks boundary explanation")
    scores = {
        "terminal": 1.0 if status_ok else 0.0,
        "required_tools": 1.0 if not any(item.startswith("required_tools") for item in violations) else 0.0,
        "constraints": 1.0 if not any(item.startswith("tool_arguments") for item in violations) else 0.0,
        "numeric_grounding": 1.0 if not any(item.startswith("numeric_grounding") for item in violations) else 0.0,
    }
    # Claim assertions bind a value to its unit and allowed numeric tolerance.
    for claim in expect.get("numeric_claims", []):
        if not isinstance(claim, dict) or not isinstance(claim.get("value"), (int, float)):
            violations.append("numeric_claims: each claim needs a numeric value")
            continue
        unit = str(claim.get("unit") or "")
        value = float(claim["value"])
        tolerance = float(claim.get("tolerance", 0))
        numbers = [float(item) for item in re.findall(r"(?<![A-Za-z])\d+(?:\.\d+)?", final_text)]
        if not any(abs(actual - value) <= tolerance for actual in numbers) or (unit and unit not in final_text):
            violations.append(f"numeric_claims: expected {value:g} {unit}".strip())
    scores["numeric_claims"] = 1.0 if not any(item.startswith("numeric_claims") for item in violations) else 0.0
    events = run.get("trace", {}).get("events", []) if isinstance(run.get("trace"), dict) else []
    model_rounds = run.get("model_rounds")
    if not isinstance(model_rounds, int):
        model_rounds = sum(1 for event in events if isinstance(event, dict) and event.get("event_type") == "model_round")
    return EvalResult(
        case_id=case.id,
        passed=not violations,
        scores=scores,
        violations=tuple(violations),
        latency_ms=float(run["latency_ms"]) if isinstance(run.get("latency_ms"), (int, float)) else None,
        model_rounds=int(model_rounds or 0),
        token_usage=dict(run.get("token_usage") or {}) if isinstance(run.get("token_usage"), dict) else {},
        run=run,
        category=case.category,
        mode=str(run.get("evaluation_mode") or "fixture"),
    )
