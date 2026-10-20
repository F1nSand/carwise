"""Structured evidence records for data-backed answer claims."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from typing import Any

from backend.data_loader import get_dataset_version


@dataclass(frozen=True)
class EvidenceRecord:
    evidence_id: str
    tool_call_id: str
    dataset_version: str
    constraint_revision: str | int | None
    metric: str
    value: Any
    unit: str
    filters: list[str]
    sample_size: int | None
    scope: str
    # Filled by the runtime after collection. Keeping the field in the
    # normalized record lets generic evidence consumers rely on one shape.
    goal_ids: list[str] = field(default_factory=list)
    # Recommendation metrics belong to a ranked candidate. These fields are
    # null for aggregate and non-recommendation evidence.
    entity_group_id: str | None = None
    entity_label: str | None = None
    brand: str | None = None
    rank: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _value_at(data: dict[str, Any], path: str) -> Any:
    value: Any = data
    for part in path.split("."):
        if isinstance(value, list) and part.isdigit():
            index = int(part)
            value = value[index] if index < len(value) else None
        elif isinstance(value, dict):
            value = value.get(part)
        else:
            return None
    return value


def _reference(prefix: str, identity: str) -> str:
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
    return f"{prefix}_{digest}"


def _metrics(
    tool_name: str,
    output: dict[str, Any],
    *,
    tool_call_id: str,
    dataset_version: str,
) -> list[dict[str, Any]]:
    """Known numeric outputs only; a csv row is always representative sample scope."""
    metrics: list[dict[str, Any]] = []
    if tool_name == "price_compare":
        n = output.get("sample_size")
        for name in ("p25_yuan", "median_yuan", "p75_yuan", "mean_yuan", "min_yuan", "max_yuan"):
            metrics.append({"metric": f"price.{name}", "unit": "CNY", "sample_size": n, "scope": "population"})
        metrics.append({"metric": "sample_size", "unit": "count", "sample_size": n, "scope": "population"})
    elif tool_name == "recommend":
        for index, item in enumerate(output.get("recommendations") or []):
            if not isinstance(item, dict):
                continue
            size = item.get("sample_size")
            series = str(item.get("series") or "").strip()
            brand = str(item.get("brand") or "").strip() or None
            rank = index + 1
            # The tool sorts the recommendation list before returning it. Bind
            # every candidate metric to that list entry and its display name.
            group_id = _reference(
                "ent",
                f"{tool_call_id}:{dataset_version}:recommendation:{rank}:{brand or ''}:{series}",
            ) if series else None
            entity = {
                "entity_group_id": group_id,
                "entity_label": series or None,
                "brand": brand,
                "rank": rank,
            }
            for name, unit in (
                ("median_wan", "wan"),
                ("p25_wan", "wan"),
                ("p75_wan", "wan"),
                ("median_year", "year"),
                ("median_mileage_wan_km", "wan_km"),
                ("score", "score"),
                ("sample_size", "count"),
            ):
                metrics.append({
                    "metric": f"recommendations.{index}.{name}",
                    "unit": unit,
                    "sample_size": size,
                    "scope": "population",
                    **entity,
                })
        metrics.extend({"metric": name, "unit": unit, "sample_size": output.get("budget_count"), "scope": "population"} for name, unit in (
            ("budget_count", "count"), ("n_candidates", "count"),
        ))
    elif tool_name == "price_decay":
        for name, unit in (("annual_depreciation_pct", "percent"), ("total_change_pct", "percent")):
            metrics.append({"metric": name, "unit": unit, "sample_size": output.get("raw_count"), "scope": "population"})
        for index, _ in enumerate(output.get("points") or []):
            for name, unit in (("median_yuan", "CNY"), ("median_wan", "wan"), ("sample_size", "count"), ("year", "year")):
                metrics.append({"metric": f"points.{index}.{name}", "unit": unit, "sample_size": _value_at(output, f"points.{index}.sample_size"), "scope": "population"})
    elif tool_name == "region_gap" and output.get("analysis_type") != "region_gap":
        metrics.append({"metric": "national_median_yuan", "unit": "CNY", "sample_size": output.get("raw_count"), "scope": "population"})
        metrics.append({"metric": "national_median_wan", "unit": "wan", "sample_size": output.get("raw_count"), "scope": "population"})
        for index, _ in enumerate(output.get("cities") or []):
            for name, unit in (("median_yuan", "CNY"), ("median_wan", "wan"), ("diff_pct", "percent"), ("sample_size", "count")):
                metrics.append({"metric": f"cities.{index}.{name}", "unit": unit, "sample_size": _value_at(output, f"cities.{index}.sample_size"), "scope": "population"})
    elif tool_name in {
        "price_distribution", "mileage_price_correlation", "model_comparison",
        "brand_profile", "age_price_relation",
    } or (tool_name == "region_gap" and output.get("analysis_type") == "region_gap"):
        # Shared AnalysisResult paths are explicitly allow-listed. Scatter
        # points and arbitrary nested fields never become answer evidence.
        analysis_type = str(output.get("analysis_type") or tool_name)
        domain = output.get("data") if isinstance(output.get("data"), dict) else {}
        stats = domain.get("data") if isinstance(domain.get("data"), dict) else {}
        n = (domain.get("counts") or {}).get("valid_rows")
        if analysis_type == "price_distribution":
            for field in ("n", "min_yuan", "max_yuan", "mean_yuan", "median_yuan", "p25_yuan", "p75_yuan"):
                metrics.append({"metric": f"data.data.{field}", "unit": "count" if field == "n" else "CNY", "sample_size": n, "scope": "population"})
            for i, cell in enumerate(stats.get("bins") or []):
                for field, unit in (("count", "count"), ("share", "ratio")):
                    metrics.append({"metric": f"data.data.bins.{i}.{field}", "unit": unit, "sample_size": _value_at(domain, f"data.bins.{i}.count"), "scope": "population"})
        elif analysis_type == "mileage_price_correlation":
            for field, unit in (("n", "count"), ("pearson_r", "correlation"), ("spearman_rho", "correlation")):
                metrics.append({"metric": f"data.data.{field}", "unit": unit, "sample_size": stats.get("n", n), "scope": "population"})
            for i, _ in enumerate(stats.get("mileage_bins") or []):
                for field, unit in (("n", "count"), ("median_price_yuan", "CNY"), ("p25_price_yuan", "CNY"), ("p75_price_yuan", "CNY")):
                    metrics.append({"metric": f"data.data.mileage_bins.{i}.{field}", "unit": unit, "sample_size": _value_at(domain, f"data.mileage_bins.{i}.n"), "scope": "population"})
        elif analysis_type in {"model_comparison", "brand_profile", "region_gap"}:
            collection = "groups" if analysis_type != "region_gap" else "cities"
            for i, item in enumerate(stats.get(collection) or []):
                if not isinstance(item, dict):
                    continue
                if analysis_type == "model_comparison":
                    identity = f"{item.get('brand_name') or ''}:{item.get('series') or ''}"
                    label = str(item.get("series") or "") or None
                    brand = str(item.get("brand_name") or "") or None
                    paths = (("n", "count"), ("price.median_yuan", "CNY"), ("price.p25_yuan", "CNY"),
                             ("price.p75_yuan", "CNY"), ("mileage.median_km", "km"), ("age.median_years", "year"))
                elif analysis_type == "brand_profile":
                    identity = str(item.get("brand_name") or item.get("category") or i)
                    label = str(item.get("brand_name") or "") or None
                    brand = label
                    paths = (("n", "count"), ("share", "ratio"), ("price.median", "CNY"),
                             ("price.p25", "CNY"), ("price.p75", "CNY"), ("series_count", "count"),
                             ("mileage.median_km", "km"), ("age.median_years", "year"))
                else:
                    identity = str(item.get("city") or i)
                    label = str(item.get("city") or "") or None
                    brand = None
                    paths = (("n", "count"), ("median_yuan", "CNY"), ("p25_yuan", "CNY"),
                             ("p75_yuan", "CNY"), ("diff_yuan", "CNY"), ("diff_pct", "percent"))
                group_id = _reference("ent", f"{tool_call_id}:{dataset_version}:{analysis_type}:{identity}")
                size = item.get("n", item.get("n"))
                for path, unit in paths:
                    metrics.append({"metric": f"data.data.{collection}.{i}.{path}", "unit": unit,
                                    "sample_size": size, "scope": "population", "entity_group_id": group_id,
                                    "entity_label": label, "brand": brand})
        elif analysis_type == "age_price_relation":
            for field, unit in (("n", "count"), ("correlation.pearson_r", "correlation"), ("correlation.spearman_rho", "correlation")):
                metrics.append({"metric": f"data.data.{field}", "unit": unit, "sample_size": stats.get("n", n), "scope": "population"})
            for i, _ in enumerate(stats.get("bins") or []):
                for field, unit in (("n", "count"), ("median_price_yuan", "CNY"), ("p25_price_yuan", "CNY"), ("p75_price_yuan", "CNY")):
                    metrics.append({"metric": f"data.data.bins.{i}.{field}", "unit": unit, "sample_size": _value_at(domain, f"data.bins.{i}.n"), "scope": "population"})
    elif tool_name == "csv_query":
        metrics.append({"metric": "match_count", "unit": "count", "sample_size": output.get("match_count"), "scope": "population"})
        for index, row in enumerate(output.get("rows") or []):
            if isinstance(row, dict):
                for name, unit in (("price_yuan", "CNY"), ("price_wan", "wan"), ("production_year", "year"), ("mileage_km", "km")):
                    metrics.append({"metric": f"rows.{index}.{name}", "unit": unit, "sample_size": len(output.get("rows") or []), "scope": "representative_sample"})
    return metrics


def evidence_from_tool_result(
    tool_name: str,
    output: dict[str, Any],
    tool_call_id: str,
    constraint_revision: str | int | None = None,
    dataset_version: str | None = None,
) -> list[dict[str, Any]]:
    """Normalize trusted domain output into JSON-safe, call-bound evidence."""
    if not isinstance(output, dict) or output.get("ok") is False or output.get("result_status") not in (None, "success"):
        return []
    data = output.get("data")
    # ToolResult's canonical business payload is `data`; legacy handlers still
    # expose fields at top level. Merge with the legacy surface winning so both
    # representations normalize to the same trusted evidence.
    payload = dict(data) if isinstance(data, dict) else {}
    payload.update(output)
    # Shared tools carry the fingerprint captured with their result; never
    # replace it with a separately observed pre-call or current file version.
    version = str(payload.get("dataset_version") or dataset_version or get_dataset_version())
    filters = [str(item) for item in payload.get("filters_applied") or []]
    records = []
    for spec in _metrics(
        tool_name, payload, tool_call_id=str(tool_call_id), dataset_version=version,
    ):
        metric = str(spec["metric"])
        unit = str(spec["unit"])
        sample_size = spec.get("sample_size")
        scope = str(spec.get("scope") or "population")
        value = _value_at(payload, metric)
        if value is None or isinstance(value, bool):
            continue
        identity = f"{tool_call_id}:{version}:{metric}"
        evidence_id = _reference("ev", identity)
        record = EvidenceRecord(
            evidence_id=evidence_id,
            tool_call_id=str(tool_call_id),
            dataset_version=version,
            constraint_revision=constraint_revision,
            metric=metric,
            value=value,
            unit=unit,
            filters=filters,
            sample_size=(int(sample_size) if sample_size is not None else None),
            scope=scope,
            entity_group_id=spec.get("entity_group_id"),
            entity_label=spec.get("entity_label"),
            brand=spec.get("brand"),
            rank=spec.get("rank"),
        )
        records.append(record.to_dict())
    return records


def evidence_summary(records: list[dict[str, Any]], limit: int = 8) -> str:
    """Small deterministic fallback suitable for a user-facing answer."""
    if not records:
        return "当前条件下没有可用的结构化数据证据，暂不能给出数值结论。"
    lines = ["可核验的数据结果："]
    for item in records[:max(0, limit)]:
        unit = {"CNY": "元", "wan": "万元", "count": "条", "percent": "%", "year": "年", "km": "公里", "score": "分"}.get(item.get("unit"), str(item.get("unit") or ""))
        value = item.get("value")
        if item.get("unit") == "CNY":
            value = round(float(value), 2)
        lines.append(f"- {item.get('metric')}：{value}{unit}（{item.get('scope')}；样本量 {item.get('sample_size') if item.get('sample_size') is not None else '未知'}）")
    lines.append("价格口径为平台挂牌价，不代表成交价；代表性样本不能代表总体统计。")
    return "\n".join(lines)
