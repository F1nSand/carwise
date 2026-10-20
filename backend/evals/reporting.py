from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from .models import EvalResult
from .scorers import extract_tool_calls


def aggregate_results(results: Iterable[EvalResult]) -> dict[str, Any]:
    items = list(results)
    if not items:
        return {"case_count": 0, "pass_rate": 0.0, "violations": [], "scores": {}, "categories": {}}
    passed = sum(item.passed for item in items)
    token_values = []
    for item in items:
        usage = item.token_usage
        if isinstance(usage.get("total_tokens"), (int, float)):
            token_values.append(int(usage["total_tokens"]))
        elif isinstance(usage.get("prompt_tokens"), (int, float)) or isinstance(usage.get("completion_tokens"), (int, float)):
            token_values.append(int(usage.get("prompt_tokens") or 0) + int(usage.get("completion_tokens") or 0))
    latencies = sorted(item.latency_ms for item in items if item.latency_ms is not None)
    def percentile(values: list[float], fraction: float) -> float | None:
        if not values:
            return None
        return float(values[min(len(values) - 1, round((len(values) - 1) * fraction))])
    categories: dict[str, list[EvalResult]] = {}
    evidence_observed: list[bool] = []
    data_tools = {"price_compare", "region_gap", "price_decay", "recommend", "csv_query"}
    for item in items:
        categories.setdefault(item.category, []).append(item)
        for result in item.run.get("tool_results", []) if isinstance(item.run.get("tool_results"), list) else []:
            if isinstance(result, dict):
                tool_name = str(result.get("tool_name") or result.get("tool") or "")
                if tool_name not in data_tools:
                    continue
                output = result.get("output") if isinstance(result.get("output"), dict) else result
                evidence_observed.append(bool(output.get("evidence")))
    complete_count = sum(str(item.run.get("status") or "") == "done" for item in items)
    return {
        "case_count": len(items),
        "passed": passed,
        "pass_rate": passed / len(items),
        "completion_rate": complete_count / len(items),
        "scores": {
            key: sum(item.scores.get(key, 0.0) for item in items) / len(items)
            for key in sorted({key for item in items for key in item.scores})
        },
        "categories": {name: {"case_count": len(group), "pass_rate": sum(item.passed for item in group) / len(group)} for name, group in sorted(categories.items())},
        "avg_model_rounds": sum(item.model_rounds for item in items) / len(items),
        "avg_tool_calls": sum(len(extract_tool_calls(item.run)) for item in items) / len(items),
        "modes": {mode: sum(item.mode == mode for item in items) for mode in sorted({item.mode for item in items})},
        "avg_tokens": sum(token_values) / len(token_values) if token_values else None,
        "evidence_pass_rate": sum(evidence_observed) / len(evidence_observed) if evidence_observed else None,
        "constraint_adherence_rate": sum(item.scores.get("constraints", 0.0) for item in items) / len(items),
        "latency_ms": {"p50": percentile(latencies, .5), "p95": percentile(latencies, .95)},
        "hard_violations": sum(1 for item in items for violation in item.violations if violation.startswith(("forbidden_tools", "out_of_domain", "numeric_claims"))),
        "violations": [
            {"case_id": item.case_id, "violation": violation}
            for item in items
            for violation in item.violations
        ],
    }


def write_report(results: Iterable[EvalResult], output_dir: Path) -> dict[str, Any]:
    items = list(results)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "results.jsonl").write_text(
        "\n".join(json.dumps({"case_id": item.case_id, "category": item.category, "mode": item.mode, "passed": item.passed, "scores": item.scores, "violations": list(item.violations), "latency_ms": item.latency_ms, "model_rounds": item.model_rounds, "token_usage": item.token_usage, "tool_call_count": len(extract_tool_calls(item.run))}, ensure_ascii=False) for item in items) + "\n",
        encoding="utf-8",
    )
    summary = aggregate_results(items)
    (output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [f"# Agent Eval Summary", "", f"- Cases: {summary['case_count']}", f"- Pass rate: {summary['pass_rate']:.1%}", ""]
    if summary.get("violations"):
        lines.extend(["## Violations", ""] + [f"- `{item['case_id']}`: {item['violation']}" for item in summary["violations"]])
    (output_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary


def compare_baseline(baseline: dict[str, Any], candidate: dict[str, Any]) -> list[str]:
    violations: list[str] = []
    def comparable(key: str) -> tuple[float | None, float | None]:
        base = baseline.get(key)
        current = candidate.get(key)
        return (float(base), float(current)) if isinstance(base, (int, float)) and isinstance(current, (int, float)) else (None, None)
    base_rate, candidate_rate = comparable("pass_rate")
    if isinstance(baseline.get("pass_rate"), (int, float)) and candidate_rate is None:
        violations.append("incomparable: candidate is missing metric pass_rate")
    if base_rate is not None and candidate_rate is not None and candidate_rate < base_rate - 0.02:
        violations.append("total pass-rate drop exceeds 2 percentage points")
    base_scores = baseline.get("scores") or {}
    candidate_scores = candidate.get("scores") or {}
    for key in ("critical", "out_of_domain"):
        if key in base_scores:
            if key not in candidate_scores or not isinstance(candidate_scores.get(key), (int, float)):
                violations.append(f"incomparable: candidate is missing metric {key}")
            elif float(candidate_scores[key]) < float(base_scores[key]) - 0.05:
                violations.append(f"critical category {key} drop exceeds 5 points")
    base_tokens, candidate_tokens = comparable("avg_tokens")
    if isinstance(baseline.get("avg_tokens"), (int, float)) and candidate_tokens is None:
        violations.append("incomparable: candidate is missing metric avg_tokens")
    if base_tokens is not None and candidate_tokens is not None and base_tokens > 0 and candidate_tokens > base_tokens * 1.15:
        violations.append("average token increase exceeds 15 percent")
    if "hard_violations" in baseline and not isinstance(candidate.get("hard_violations"), (int, float)):
        violations.append("incomparable: candidate is missing metric hard_violations")
    if isinstance(candidate.get("hard_violations"), (int, float)) and candidate["hard_violations"] > 0:
        violations.append("hard safety violations present")
    return violations
