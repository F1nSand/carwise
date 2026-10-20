"""Deterministic context budgeting that never mutates the persisted Agent state."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from backend.config import (
    CONTEXT_MAX_INPUT_TOKENS,
    CONTEXT_MAX_CONTEXT_TOKENS,
    CONTEXT_MAX_SINGLE_TOOL_RESULT_TOKENS,
    CONTEXT_MIN_RECENT_TURNS,
    CONTEXT_RESERVED_OUTPUT_TOKENS,
    CONTEXT_SAFETY_MARGIN_TOKENS,
)


@dataclass(frozen=True)
class ContextBudget:
    max_context_tokens: int
    reserved_output_tokens: int
    safety_margin_tokens: int
    min_recent_turns: int
    max_single_tool_result_tokens: int
    max_input_tokens: int | None = None

    def __post_init__(self) -> None:
        if self.max_context_tokens <= 0:
            raise ValueError("max_context_tokens 必须为正数")
        if self.reserved_output_tokens < 0 or self.safety_margin_tokens < 0:
            raise ValueError("输出预留和安全余量不能为负数")
        if self.min_recent_turns < 0:
            raise ValueError("min_recent_turns 不能为负数")
        if self.max_single_tool_result_tokens <= 0:
            raise ValueError("max_single_tool_result_tokens 必须为正数")
        if self.max_input_tokens is not None and self.max_input_tokens <= 0:
            raise ValueError("max_input_tokens 必须为正数")
        if self.input_tokens <= 0:
            raise ValueError("上下文输入预算必须为正数")

    @property
    def input_tokens(self) -> int:
        available = self.max_context_tokens - self.reserved_output_tokens - self.safety_margin_tokens
        return min(available, self.max_input_tokens) if self.max_input_tokens is not None else available

    def to_config(self) -> dict[str, int]:
        return {
            "max_context_tokens": self.max_context_tokens,
            "reserved_output_tokens": self.reserved_output_tokens,
            "safety_margin_tokens": self.safety_margin_tokens,
            "min_recent_turns": self.min_recent_turns,
            "max_single_tool_result_tokens": self.max_single_tool_result_tokens,
            "max_input_tokens": self.max_input_tokens or self.input_tokens,
        }

    @classmethod
    def from_config(cls, value: Mapping[str, Any]) -> "ContextBudget":
        return cls(
            max_context_tokens=int(value["max_context_tokens"]),
            reserved_output_tokens=int(value["reserved_output_tokens"]),
            safety_margin_tokens=int(value["safety_margin_tokens"]),
            min_recent_turns=int(value["min_recent_turns"]),
            max_single_tool_result_tokens=int(value["max_single_tool_result_tokens"]),
            max_input_tokens=int(value["max_input_tokens"]) if value.get("max_input_tokens") is not None else None,
        )


@dataclass(frozen=True)
class ContextWindow:
    messages: list[dict[str, Any]]
    estimated_input_tokens: int
    omitted_message_count: int
    compacted_tool_result_count: int


class ContextInputTooLarge(ValueError):
    code = "context_input_too_large"

    def __init__(self) -> None:
        super().__init__(self.code)


class TokenEstimator:
    """Conservative, replaceable estimator based on UTF-8 JSON bytes."""

    def estimate_messages(self, messages: Sequence[Mapping[str, Any]]) -> int:
        serialized = json.dumps(
            list(messages),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        # One byte per estimated token intentionally errs high for Chinese text
        # and leaves a small fixed allowance for per-message protocol framing.
        return len(serialized.encode("utf-8")) + 4 * len(messages)

    def estimate_tools(self, tools: Sequence[Mapping[str, Any]]) -> int:
        serialized = json.dumps(
            list(tools),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return len(serialized.encode("utf-8")) + 4 * len(tools)

    def estimate_request(self, messages: Sequence[Mapping[str, Any]], tools: Sequence[Mapping[str, Any]]) -> int:
        # This remains a documented byte-based approximation, not the provider's tokenizer.
        return self.estimate_messages(messages) + self.estimate_tools(tools) + 128


DEFAULT_CONTEXT_BUDGET = ContextBudget(
    max_context_tokens=CONTEXT_MAX_CONTEXT_TOKENS,
    reserved_output_tokens=CONTEXT_RESERVED_OUTPUT_TOKENS,
    safety_margin_tokens=CONTEXT_SAFETY_MARGIN_TOKENS,
    min_recent_turns=CONTEXT_MIN_RECENT_TURNS,
    max_single_tool_result_tokens=CONTEXT_MAX_SINGLE_TOOL_RESULT_TOKENS,
    max_input_tokens=CONTEXT_MAX_INPUT_TOKENS,
)


def user_input_exceeds_budget(
    user_text: str,
    *,
    system_prompt: str,
    budget: ContextBudget = DEFAULT_CONTEXT_BUDGET,
    estimator: TokenEstimator | None = None,
    tools: Sequence[Mapping[str, Any]] = (),
) -> bool:
    estimator = estimator or TokenEstimator()
    minimum_messages = [
        {"role": "system", "content": str(system_prompt or "")},
        {"role": "user", "content": str(user_text or "")},
    ]
    return estimator.estimate_request(minimum_messages, tools) > budget.input_tokens


_STAT_KEYS = (
    "series",
    "confidence",
    "budget_count",
    "n_candidates",
    "sample_size",
    "match_count",
    "median_wan",
    "p25_wan",
    "p75_wan",
    "mean_wan",
    "min_wan",
    "max_wan",
    "median_year",
    "median_mileage_wan_km",
    "iqr_ratio",
    "result_status",
)
_PRICE_STAT_KEYS = ("median_wan", "p25_wan", "p75_wan", "mean_wan", "min_wan", "max_wan", "iqr_ratio")
_RECOMMENDATION_NUMERIC_FIELDS = {
    "median_wan", "p25_wan", "p75_wan", "median_year",
    "median_mileage_wan_km", "score", "sample_size",
}
_SHARED_ANALYSIS_TOOLS = {
    "price_distribution", "mileage_price_correlation", "model_comparison",
    "brand_profile", "region_gap", "age_price_relation",
}


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _json_content(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or ""))
    except (TypeError, ValueError):
        return {"note": str(value or "")}
    return dict(parsed) if isinstance(parsed, Mapping) else {"note": str(value or "")}


def _compact_series_recovery(tool_name: str, domain: Mapping[str, Any],
                             estimator: TokenEstimator, limit: int) -> str:
    """Preserve catalog keyword repair details without fabricating analysis evidence."""
    source_candidates = domain.get("candidate_selectors")
    source_candidates = source_candidates if isinstance(source_candidates, list) else []
    candidates = [
        {key: str(item[key]) for key in ("brand_name", "series") if item.get(key) is not None}
        for item in source_candidates
        if isinstance(item, Mapping) and item.get("brand_name") and item.get("series")
    ]
    omitted = max(0, len(candidates) - 12)
    candidates = candidates[:12]
    filters = deepcopy(dict(_mapping(domain.get("normalized_filters"))))
    candidate_values = domain.get("candidate_values")
    candidate_values = [str(value) for value in candidate_values if value is not None] if isinstance(candidate_values, list) else []
    omitted_values = max(0, len(candidate_values) - 20)
    candidate_values = candidate_values[:20]
    result: dict[str, Any] = {
        "ok": False,
        "tool_name": tool_name,
        "result_status": str(domain.get("result_status") or domain.get("status") or "error")[:40],
        "reason_code": str(domain.get("reason_code") or "unknown_series")[:80],
        "keyword_field": domain.get("keyword_field"),
        "requested_keyword": domain.get("requested_keyword"),
        "candidate_values": candidate_values,
        "omitted_candidate_value_count": omitted_values,
        "requested_series": domain.get("requested_series"),
        "candidate_selectors": candidates,
        "omitted_candidate_count": omitted,
        "repair_hint": str(domain.get("repair_hint") or "请通过 ask_user 确认真实车系，并保留已确认筛选条件。"),
        "dataset_version": domain.get("dataset_version"),
        "normalized_filters": filters,
    }
    if domain.get("city") is not None:
        result["city"] = domain.get("city")

    def encode() -> str:
        return json.dumps(result, ensure_ascii=False, separators=(",", ":"), default=str)

    def fits() -> bool:
        return estimator.estimate_messages([{"role": "tool", "content": encode()}]) <= limit

    while result["candidate_selectors"] and not fits():
        result["candidate_selectors"].pop()
        result["omitted_candidate_count"] += 1

    while result["candidate_values"] and not fits():
        result["candidate_values"].pop()
        result["omitted_candidate_value_count"] += 1

    if not fits():
        hint = result["repair_hint"]
        while hint and not fits():
            hint = hint[: max(0, len(hint) // 2)]
            result["repair_hint"] = hint

    # Keep every city while possible. If a very small limit forces other
    # confirmed filters out, report their names so the omission is explicit.
    if not fits():
        omitted_filters = []
        for key in list(filters):
            if key in {"cities", "city", "city_name"}:
                continue
            filters.pop(key)
            omitted_filters.append(key)
            result["omitted_filter_keys"] = omitted_filters
            if fits():
                break

    if not fits() and isinstance(filters.get("cities"), list):
        cities = filters["cities"]
        while cities and not fits():
            cities.pop()
            filters["omitted_city_count"] = len(_mapping(domain.get("normalized_filters")).get("cities", [])) - len(cities)
    return encode()


def _compact_tool_content(message: Mapping[str, Any], estimator: TokenEstimator, limit: int) -> str:
    tool_name = str(message.get("name") or "tool")[:80]
    output = _json_content(message.get("content"))
    if tool_name == "read_report":
        return _compact_report_tool_content(output, estimator, limit)
    # Prefer top-level legacy fields when present, while accepting canonical
    # ToolResult payloads that keep business fields under `data` only.
    domain = dict(_mapping(output.get("data")))
    domain.update(output)
    if (domain.get("ok") is False and domain.get("result_status") == "error"
            and domain.get("reason_code") in {
                "ambiguous_series", "unknown_series", "ambiguous_brand", "unknown_brand",
                "ambiguous_city", "unknown_city",
            }):
        return _compact_series_recovery(tool_name, domain, estimator, limit)
    ok = output.get("ok")
    status = str(output.get("result_status") or output.get("status") or ("done" if ok is not False else "error"))[:40]
    summary: dict[str, Any] = {"tool_name": tool_name, "status": status}
    if domain.get("reason_code") is not None:
        summary["reason_code"] = str(domain["reason_code"])[:80]
    # Recommendations have several different counts (budget matches,
    # qualifying series, and returned ranked candidates). Preserve their
    # actual names below instead of collapsing them into an ambiguous number.
    if tool_name != "recommend":
        for key in ("match_count", "sample_size", "budget_count", "n_candidates", "total_count"):
            value = domain.get(key)
            if _is_number(value):
                summary["sample_count"] = max(0, int(value))
                break

    statistics = {
        key: domain[key]
        for key in _STAT_KEYS
        if key in domain and isinstance(domain[key], (str, int, float, bool, type(None)))
    }
    price = _mapping(domain.get("price"))
    if price:
        statistics.update(
            {
                f"price_{key}": price[key]
                for key in _PRICE_STAT_KEYS
                if key in price and isinstance(price[key], (int, float)) and not isinstance(price[key], bool)
            }
        )
    if statistics:
        summary["statistics"] = statistics

    if tool_name in _SHARED_ANALYSIS_TOOLS:
        analysis = _mapping(domain.get("data"))
        counts = _mapping(analysis.get("counts"))
        analysis_view: dict[str, Any] = {
            "analysis_type": analysis.get("analysis_type"),
            "status": analysis.get("status"),
            "reason_code": analysis.get("reason_code"),
            "dataset_version": analysis.get("dataset_version"),
            "normalized_filters": analysis.get("normalized_filters"),
            "counts": dict(counts),
            "limitations": list(analysis.get("limitations") or [])[:12],
            "methodology": analysis.get("methodology"),
        }
        raw_data = _mapping(analysis.get("data"))
        analysis_type = str(analysis.get("analysis_type") or tool_name)
        # These are compact statistical summaries for model reasoning. In
        # particular, scatter.points is deliberately excluded; the full,
        # capped display data stays attached to the report result.
        if analysis_type == "price_distribution":
            data_view = {key: raw_data.get(key) for key in
                         ("n", "min_yuan", "max_yuan", "mean_yuan", "median_yuan", "p25_yuan", "p75_yuan")}
            data_view["bins"] = list(raw_data.get("bins") or [])[:40]
        elif analysis_type == "mileage_price_correlation":
            data_view = {key: raw_data.get(key) for key in ("n", "pearson_r", "spearman_rho", "correlation_reason")}
            data_view["mileage_bins"] = list(raw_data.get("mileage_bins") or [])[:20]
            scatter = _mapping(raw_data.get("scatter"))
            data_view["scatter"] = {key: scatter.get(key) for key in ("total_points", "display_points", "sampled")}
        elif analysis_type == "model_comparison":
            data_view = {"groups": list(raw_data.get("groups") or [])[:6]}
        elif analysis_type == "brand_profile":
            data_view = {"denominator_n": raw_data.get("denominator_n"), "groups": list(raw_data.get("groups") or [])[:22]}
        elif analysis_type == "region_gap":
            data_view = {key: raw_data.get(key) for key in ("baseline_median_yuan", "baseline_n", "baseline_label", "valid_city_count")}
            cities = list(raw_data.get("cities") or [])
            data_view["cities"] = cities[:40]
            data_view["omitted_city_count"] = max(0, len(cities) - len(data_view["cities"]))
        else:
            data_view = {key: raw_data.get(key) for key in ("n", "missing_age_n", "correlation")}
            data_view["bins"] = list(raw_data.get("bins") or [])[:40]
            scatter = _mapping(raw_data.get("scatter"))
            data_view["scatter"] = {key: scatter.get(key) for key in ("total_points", "display_points", "sampled")}
        analysis_view["data"] = data_view
        summary["analysis_result"] = analysis_view
        filters = analysis.get("filters_applied")
        if isinstance(filters, list):
            summary["filters_applied"] = [str(value)[:200] for value in filters[:12]]

    recommendations = domain.get("recommendations")
    note = domain.get("note")
    if note and tool_name != "recommend":
        summary["note"] = str(note)[:240]

    # Keep enough of each structured evidence record for the model to submit a
    # valid finalize_answer call after large tool payloads are compacted. These
    # fields are the claim contract; raw rows and verbose evidence metadata are
    # deliberately omitted from the temporary model view.
    raw_evidence = domain.get("evidence")
    if isinstance(raw_evidence, list):
        compact_evidence = []
        for raw in raw_evidence:
            item = _mapping(raw)
            evidence_id = item.get("evidence_id")
            metric = item.get("metric")
            if not evidence_id or not metric:
                continue
            fields = (
                # The named recommendation group below already binds evidence
                # refs to series, brand, and rank. Repeating that metadata,
                # along with server-only revision/version data, for every
                # metric made a six-kilobyte result expose only one candidate.
                # Keep the compact evidence view sufficient to choose trusted
                # refs; full records remain in the authoritative runtime state.
                ("evidence_id", "metric", "value", "unit", "scope", "entity_group_id")
                if tool_name == "recommend"
                else (
                    "evidence_id", "metric", "value", "unit", "scope",
                    "sample_size", "dataset_version", "constraint_revision",
                    "entity_group_id", "entity_label", "brand", "rank",
                )
            )
            record = {
                key: item[key]
                for key in fields
                if key in item and isinstance(item[key], (str, int, float, bool, type(None)))
            }
            if tool_name != "recommend" and isinstance(item.get("goal_ids"), list):
                record["goal_ids"] = [str(value) for value in item["goal_ids"] if value is not None][:16]
            if tool_name == "recommend" and item.get("entity_group_id"):
                # Recommendation filters are shared by every candidate and
                # displayed once at the result level below.
                pass
            elif isinstance(item.get("filters"), list):
                record["filters"] = [str(value)[:120] for value in item["filters"][:5]]
            compact_evidence.append(record)
        if compact_evidence:
            summary["evidence"] = compact_evidence

    recommendation_groups: list[dict[str, Any]] = []
    if tool_name == "recommend" and isinstance(recommendations, list):
        filters = domain.get("filters_applied")
        if isinstance(filters, list):
            summary["filters_applied"] = [str(value)[:200] for value in filters[:12]]
        records = summary.get("evidence", [])
        raw_records = [record for record in records if record.get("entity_group_id")]
        aggregate_records = [
            record for record in records
            if not record.get("entity_group_id")
            and not str(record.get("metric") or "").startswith("recommendations.")
        ]
        # Keep only complete ranked entities. If the event payload's evidence
        # cap cut through a candidate, do not expose that candidate's remaining
        # raw values without matching stable evidence refs. Because the source
        # list is rank-ordered, stop at the first incomplete candidate so the
        # visible list remains a prefix of the ranked result.
        for index, raw in enumerate(recommendations):
            item = _mapping(raw)
            matching = [
                record for record in raw_records
                if str(record.get("metric") or "").startswith(f"recommendations.{index}.")
            ]
            if not matching:
                break
            group_id = str(matching[0].get("entity_group_id") or "")
            if not group_id or any(str(record.get("entity_group_id") or "") != group_id for record in matching):
                break
            expected_metrics = {
                f"recommendations.{index}.{field}"
                for field in _RECOMMENDATION_NUMERIC_FIELDS
                if _is_number(item.get(field))
            }
            present_metrics = {str(record.get("metric")) for record in matching}
            if not expected_metrics or not expected_metrics.issubset(present_metrics):
                break
            first = matching[0]
            entity_label = str(first.get("entity_label") or item.get("series") or "").strip()
            if not entity_label:
                break
            recommendation_groups.append(
                {
                    "entity_group_id": group_id,
                    "rank": first.get("rank") if isinstance(first.get("rank"), int) else index + 1,
                    "series": entity_label,
                    "brand": first.get("brand") or item.get("brand"),
                    # Values and units appear only in evidence entries so each
                    # numeric fact has one canonical ref-bearing representation.
                    "evidence_refs": [str(record["evidence_id"]) for record in matching],
                }
            )
        if recommendation_groups:
            summary["recommendations"] = recommendation_groups
        summary["entity_group_refs"] = [group["entity_group_id"] for group in recommendation_groups]
        summary["total_count"] = len(recommendations)
        summary["visible_count"] = len(recommendation_groups)
        summary["omitted_count"] = max(0, len(recommendations) - len(recommendation_groups))
        summary["evidence"] = aggregate_records + [
            record for record in records
            if record.get("entity_group_id") in {group["entity_group_id"] for group in recommendation_groups}
        ]

    def encode() -> str:
        return json.dumps(summary, ensure_ascii=False, separators=(",", ":"), default=str)

    if tool_name == "recommend" and summary.get("recommendations"):
        # Trim candidates as complete entity/evidence groups. Never pop an
        # arbitrary evidence record: that would leave a rank or value orphaned.
        # First remove lower-priority metrics uniformly across all visible
        # candidates. This lets a small answer budget preserve names, median,
        # price interval, and sample size for more of the ranked list.
        omitted_metrics: list[str] = []
        for field in ("score", "median_mileage_wan_km", "median_year"):
            if estimator.estimate_messages([{"role": "tool", "content": encode()}]) <= limit:
                break
            summary["evidence"] = [
                record for record in summary.get("evidence", [])
                if str(record.get("metric") or "").rsplit(".", 1)[-1] != field
            ]
            for group in summary["recommendations"]:
                group["evidence_refs"] = [
                    ref for ref in group.get("evidence_refs", [])
                    if not any(
                        record.get("evidence_id") == ref
                        and str(record.get("metric") or "").rsplit(".", 1)[-1] == field
                        for record in raw_records
                    )
                ]
            omitted_metrics.append(field)
        if omitted_metrics:
            summary["omitted_metrics"] = omitted_metrics
        while summary.get("recommendations") and estimator.estimate_messages([{"role": "tool", "content": encode()}]) > limit:
            summary["recommendations"].pop()
            keep_groups = {group["entity_group_id"] for group in summary["recommendations"]}
            summary["evidence"] = [
                record for record in summary.get("evidence", [])
                if not record.get("entity_group_id") or record.get("entity_group_id") in keep_groups
            ]
            summary["visible_count"] = len(summary["recommendations"])
            summary["omitted_count"] = max(0, int(summary.get("total_count") or 0) - summary["visible_count"])
            summary["entity_group_refs"] = [group["entity_group_id"] for group in summary["recommendations"]]
        if not summary.get("recommendations"):
            summary.pop("recommendations", None)
    else:
        while summary.get("recommendations") and estimator.estimate_messages([{"role": "tool", "content": encode()}]) > limit:
            summary["recommendations"].pop()
            if not summary["recommendations"]:
                summary.pop("recommendations", None)
    # Evidence identifiers are required to produce a validated final answer.
    # If a tool produced many metrics, retain the leading records that fit
    # rather than discarding the entire evidence array along with raw output.
    if tool_name != "recommend":
        while len(summary.get("evidence", [])) > 1 and estimator.estimate_messages([{"role": "tool", "content": encode()}]) > limit:
            summary["evidence"].pop()
    if estimator.estimate_messages([{"role": "tool", "content": encode()}]) > limit:
        summary.pop("note", None)
    if estimator.estimate_messages([{"role": "tool", "content": encode()}]) > limit:
        summary.pop("statistics", None)
    if estimator.estimate_messages([{"role": "tool", "content": encode()}]) > limit:
        summary.pop("reason_code", None)
    # If one evidence record plus the summary exceeds the limit, drop optional
    # metadata and retain the smallest claimable record. Never truncate JSON or
    # an evidence identifier mid-string.
    if tool_name != "recommend" and estimator.estimate_messages([{"role": "tool", "content": encode()}]) > limit and summary.get("evidence"):
        evidence = summary["evidence"]
        summary.pop("statistics", None)
        summary.pop("note", None)
        summary.pop("reason_code", None)
        summary.pop("sample_count", None)
        summary.pop("recommendations", None)
        summary["evidence"] = [
            {
                key: record[key]
                for key in ("evidence_id", "metric", "value", "unit", "scope", "sample_size")
                if key in record
            }
            for record in evidence[:1]
        ]
    return encode()


def _compact_report_tool_content(output: Mapping[str, Any], estimator: TokenEstimator, limit: int) -> str:
    """Keep report identity, source/version and the most useful saved content."""
    nested = _mapping(output.get("data"))
    source = dict(nested)
    source.update(output)
    body = source.get("content") if isinstance(source.get("content"), Mapping) else {}
    compact_body = {
        key: body[key]
        for key in ("summaryTitle", "question", "answer", "summaryText", "reliability", "methodology",
                    "filtersApplied", "limitations", "answerEvidence")
        if key in body
    }
    evidence = _mapping(compact_body.get("answerEvidence"))
    if isinstance(evidence.get("claims"), list):
        evidence = {**evidence, "claims": list(evidence["claims"][:12])}
        compact_body["answerEvidence"] = evidence
    result = {
        "ok": bool(source.get("ok", True)),
        "source": str(source.get("source") or "historical_report"),
        "report_id": source.get("report_id"),
        "version": source.get("version"),
        "origin_run_id": source.get("origin_run_id"),
        "dataset_version": source.get("dataset_version"),
        "context_text": str(source.get("context_text") or ""),
        "content": compact_body,
        "evidence_refs": list(source.get("evidence_refs") or [])[:32],
    }

    def encode() -> str:
        return json.dumps(result, ensure_ascii=False, separators=(",", ":"), default=str)

    while estimator.estimate_messages([{"role": "tool", "content": encode()}]) > limit:
        claims = result["content"].get("answerEvidence", {}).get("claims")
        if isinstance(claims, list) and len(claims) > 1:
            result["content"]["answerEvidence"]["claims"] = claims[:-1]
            result["evidence_refs"] = result["evidence_refs"][:-1]
            continue
        for key in ("filtersApplied", "limitations", "methodology", "reliability", "question"):
            if key in result["content"]:
                result["content"].pop(key)
                break
        else:
            if "context_text" in result and result["context_text"]:
                result["context_text"] = result["context_text"][: max(0, len(result["context_text"]) // 2)]
            elif "answerEvidence" in result["content"]:
                result["content"].pop("answerEvidence", None)
                result["evidence_refs"] = []
            elif "answer" in result["content"]:
                result["content"].pop("answer", None)
            elif "summaryText" in result["content"]:
                result["content"].pop("summaryText", None)
            else:
                break
    return encode()


def _turn_groups(messages: Sequence[Mapping[str, Any]]) -> list[list[int]]:
    groups: list[list[int]] = []
    prelude: list[int] = []
    current: list[int] | None = None
    for index, message in enumerate(messages):
        if message.get("role") == "system":
            continue
        if message.get("role") == "user":
            if current is not None:
                groups.append(current)
                current = []
            elif prelude:
                current = prelude
                prelude = []
            else:
                current = []
        if current is None:
            prelude.append(index)
        else:
            current.append(index)
    if current is not None:
        groups.append(current)
    elif prelude:
        groups.append(prelude)
    return groups


def _current_user_index(messages: Sequence[Mapping[str, Any]], current_run_start: int) -> int | None:
    if 0 <= current_run_start < len(messages) and messages[current_run_start].get("role") == "user":
        return current_run_start
    candidates = [index for index, message in enumerate(messages) if message.get("role") == "user"]
    if not candidates:
        return None
    after_start = [index for index in candidates if index >= current_run_start]
    return after_start[-1] if after_start else candidates[-1]


def build_context_window(
    messages: Sequence[Mapping[str, Any]],
    *,
    current_run_start: int,
    budget: ContextBudget,
    estimator: TokenEstimator,
    tools: Sequence[Mapping[str, Any]] = (),
) -> ContextWindow:
    """Build a temporary, legal model request view without mutating source messages."""
    original = [dict(message) for message in messages]
    selected: dict[int, dict[str, Any]] = {index: dict(message) for index, message in enumerate(original)}
    current_user = _current_user_index(original, int(current_run_start))
    protected_seed = [dict(message) for index, message in enumerate(original) if message.get("role") == "system"]
    if current_user is not None:
        protected_seed.append(original[current_user])
    if estimator.estimate_request(protected_seed, tools) > budget.input_tokens:
        raise ContextInputTooLarge()

    compacted: set[int] = set()

    def ordered_messages() -> list[dict[str, Any]]:
        return [selected[index] for index in sorted(selected)]

    def estimated() -> int:
        return estimator.estimate_request(ordered_messages(), tools)

    tool_indices = [index for index, message in enumerate(original) if message.get("role") == "tool"]
    for index in tool_indices:
        message = selected.get(index)
        if message is None:
            continue
        # Recommendation payloads have a legacy duplicated `data` projection.
        # Always replace the model-view with the grouped, ref-bearing shape;
        # otherwise a small result can bypass compaction and keep both copies.
        if str(message.get("name") or "") == "recommend" or estimator.estimate_messages([message]) > budget.max_single_tool_result_tokens:
            replacement = dict(message)
            replacement["content"] = _compact_tool_content(message, estimator, budget.max_single_tool_result_tokens)
            selected[index] = replacement
            compacted.add(index)

    groups = _turn_groups(original)
    user_groups = [group for group in groups if any(original[index].get("role") == "user" for index in group)]
    protected_groups = {tuple(group) for group in user_groups[-budget.min_recent_turns :]} if budget.min_recent_turns else set()
    for group in user_groups:
        if current_user in group:
            protected_groups.add(tuple(group))
    for group in groups:
        if tuple(group) not in protected_groups and estimated() > budget.input_tokens:
            for index in group:
                selected.pop(index, None)

    if estimated() > budget.input_tokens:
        for index in tool_indices:
            message = selected.get(index)
            if message is None or index in compacted:
                continue
            replacement = dict(message)
            replacement["content"] = _compact_tool_content(message, estimator, budget.max_single_tool_result_tokens)
            selected[index] = replacement
            compacted.add(index)
            if estimated() <= budget.input_tokens:
                break

    if estimated() > budget.input_tokens:
        for group in groups:
            if tuple(group) not in protected_groups and any(index in selected for index in group):
                for index in group:
                    selected.pop(index, None)
                if estimated() <= budget.input_tokens:
                    break

    final_messages = ordered_messages()
    final_estimate = estimator.estimate_request(final_messages, tools)
    if final_estimate > budget.input_tokens:
        raise ContextInputTooLarge()
    return ContextWindow(
        messages=final_messages,
        estimated_input_tokens=final_estimate,
        omitted_message_count=len(original) - len(selected),
        compacted_tool_result_count=len(compacted.intersection(selected)),
    )


def accumulate_usage(
    aggregate: Mapping[str, Any] | None,
    current: Mapping[str, Any],
) -> dict[str, Any]:
    """Sum model token counters while retaining the latest and provider metrics."""
    previous = _mapping(aggregate)
    current_usage = _mapping(current)
    core = ("prompt_tokens", "completion_tokens", "total_tokens")
    result: dict[str, Any] = {}
    for key in core:
        old = previous.get(key, 0)
        new = current_usage.get(key, 0)
        old_number = old if isinstance(old, (int, float)) and not isinstance(old, bool) else 0
        new_number = new if isinstance(new, (int, float)) and not isinstance(new, bool) else 0
        result[key] = old_number + new_number

    provider_metrics = dict(_mapping(previous.get("provider_metrics")))
    for key, value in current_usage.items():
        if key in core:
            continue
        if isinstance(value, Mapping):
            for nested_key, nested_value in value.items():
                metric_key = f"{key}.{nested_key}"
                if isinstance(nested_value, (int, float)) and not isinstance(nested_value, bool):
                    provider_metrics[metric_key] = provider_metrics.get(metric_key, 0) + nested_value
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            provider_metrics[key] = provider_metrics.get(key, 0) + value

    cache_hit = current_usage.get("prompt_cache_hit") is True or any(
        "cache_hit" in str(key).lower() and isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0
        for key, value in current_usage.items()
    )
    result["model_calls"] = int(previous.get("model_calls") or 0) + 1
    result["cache_hit_calls"] = int(previous.get("cache_hit_calls") or 0) + int(cache_hit)
    result["last_model_usage"] = dict(current_usage)
    result["provider_metrics"] = provider_metrics
    return result
