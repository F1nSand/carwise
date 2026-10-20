"""Agent adapters for the shared deterministic analysis service."""

from __future__ import annotations

from typing import Any

from backend.analysis.contracts import AnalysisInputError
from backend.analysis.service import run_analysis
import backend.data_loader as data_loader

def get_dataset_snapshot():
    return data_loader.get_dataset_snapshot()
from backend.tools.catalog_resolution import resolve_tool_filters


def run_shared_analysis(analysis_type: str, filters: dict | None = None, params: dict | None = None,
                       snapshot=None) -> dict[str, Any]:
    """Run a shared analysis and expose the existing normalized tool contract."""
    current_snapshot = snapshot or get_dataset_snapshot()
    try:
        resolved_filters, catalog_error = resolve_tool_filters(current_snapshot, filters, analysis_type)
        if catalog_error:
            return catalog_error
        result = run_analysis(analysis_type, filters=resolved_filters, params=params, snapshot=current_snapshot)
    except AnalysisInputError as exc:
        status = "unsupported" if exc.code in {"unsupported_filter", "incompatible_filters"} else "invalid_arguments"
        return {"ok": False, "tool": analysis_type, "result_status": status,
                "reason_code": exc.code,
                "error_code": "unsupported_filter" if status == "unsupported" else "invalid_arguments",
                "error": str(exc)}
    except ValueError as exc:
        return {"ok": False, "tool": analysis_type, "result_status": "invalid_arguments",
                "reason_code": "invalid_params", "error_code": "invalid_arguments", "error": str(exc)}

    status = str(result.get("status") or "error")
    legacy_status = status if status in {"success", "empty", "unsupported", "error"} else "error"
    return {
        "ok": legacy_status in {"success", "empty"},
        "tool": analysis_type,
        "result_status": legacy_status,
        "reason_code": result.get("reason_code"),
        "error_code": "unsupported_filter" if legacy_status == "unsupported" else None,
        "analysis_type": analysis_type,
        "dataset_version": result.get("dataset_version"),
        "filters_applied": result.get("filters_applied", []),
        # Keep the complete bounded AnalysisResult available to reports. The
        # model-facing view is separately compacted by context_budget.
        "data": result,
    }


def price_distribution(filters=None, params=None):
    return run_shared_analysis("price_distribution", filters, params)


def mileage_price_correlation(filters=None, params=None):
    return run_shared_analysis("mileage_price_correlation", filters, params)


def model_comparison(filters=None, params=None):
    return run_shared_analysis("model_comparison", filters, params)


def brand_profile(filters=None, params=None):
    return run_shared_analysis("brand_profile", filters, params)


def age_price_relation(filters=None, params=None):
    return run_shared_analysis("age_price_relation", filters, params)



def _resolve_region_series(snapshot, series: str, brands: list[str]):
    from backend.tools.catalog_resolution import resolve_keyword
    resolved, error = resolve_keyword(snapshot, "series", series, {"brands": brands}, "region_gap")
    if error:
        ambiguous = error["reason_code"] == "ambiguous_series"
        selectors = error.get("candidate_selectors", [])
        return ("ambiguous" if ambiguous else "unknown"), [], selectors
    return "resolved", [resolved], [resolved]

def shared_region_gap(series: str, year: int | None = None, min_city_n: int = 15,
                      filters: dict | None = None) -> dict[str, Any]:
    """Compatibility wrapper: legacy selectors must agree with shared filters."""
    if filters is None:
        # Calls using the historic schema keep their exact wrapper contract.
        from backend.tools.region_gap import region_price_gap
        return region_price_gap(series, year=year, min_city_n=min_city_n)
    from backend.analysis.contracts import normalize_filters

    try:
        normalized = normalize_filters(filters)
    except AnalysisInputError as exc:
        unsupported = exc.code in {"unsupported_filter", "incompatible_filters"}
        return {"ok": False, "tool": "region_gap",
                "result_status": "unsupported" if unsupported else "invalid_arguments",
                "reason_code": exc.code,
                "error_code": "unsupported_filter" if unsupported else "invalid_arguments",
                "error": str(exc)}
    # Include the legacy year before any catalog error can return. A name
    # correction must retain the same year as the original query.
    if year is not None:
        if ((normalized["year_min"] is not None and year < normalized["year_min"])
                or (normalized["year_max"] is not None and year > normalized["year_max"])):
            return {"ok": False, "tool": "region_gap", "result_status": "unsupported",
                    "reason_code": "incompatible_filters", "error_code": "unsupported_filter",
                    "error": "legacy year conflicts with filters year range"}
        normalized["year_min"] = normalized["year_max"] = year
    snapshot = get_dataset_snapshot()
    from backend.tools.catalog_resolution import resolve_keyword, resolve_tool_filters
    normalized, catalog_error = resolve_tool_filters(snapshot, normalized, "region_gap")
    if catalog_error:
        return catalog_error
    selectors = normalized["series_selectors"]
    if len(selectors) > 1:
        return {"ok": False, "tool": "region_gap", "result_status": "error", "reason_code": "ambiguous_series", "keyword_field": "series", "requested_keyword": series, "requested_series": series, "candidate_values": [x["series"] for x in selectors], "candidate_selectors": selectors, "normalized_filters": normalized, "dataset_version": snapshot.version, "repair_hint": "请通过 ask_user 确认一个真实车系，并保留其他筛选条件。"}
    allowed_brands = [item["brand_name"] for item in selectors] if selectors else normalized["brands"]
    resolved, catalog_error = resolve_keyword(snapshot, "series", series,
                                              {**normalized, "brands": allowed_brands}, "region_gap")
    if catalog_error:
        catalog_error["requested_series"] = series
        if catalog_error.get("reason_code") == "ambiguous_series":
            catalog_error["available_brands"] = sorted({x["brand_name"] for x in catalog_error.get("candidate_selectors", [])})
        return catalog_error
    if selectors and resolved != selectors[0]:
        return {"ok": False, "tool": "region_gap", "result_status": "unsupported",
                "reason_code": "incompatible_filters", "error_code": "unsupported_filter",
                "error": "legacy series conflicts with filters.series_selectors"}
    normalized["series_selectors"] = [resolved]
    result = run_shared_analysis("region_gap", normalized, {"min_city_n": min_city_n}, snapshot=snapshot)
    # Keep the historic region_gap fields for older report projections while
    # the nested AnalysisResult remains the authoritative shared contract.
    analysis = result.get("data") if isinstance(result.get("data"), dict) else {}
    region = analysis.get("data") if isinstance(analysis.get("data"), dict) else {}
    baseline = region.get("baseline_median_yuan")
    old_cities = []
    for cell in region.get("cities") or []:
        if not isinstance(cell, dict) or not cell.get("eligible"):
            continue
        median = cell.get("median_yuan")
        diff = cell.get("diff_yuan")
        old_cities.append({
            "city": cell.get("city"), "sample_size": cell.get("n"),
            "median_yuan": median,
            "median_wan": round(float(median) / 10000, 2) if median is not None else None,
            "diff_vs_national_yuan": diff,
            "diff_vs_national_wan": round(float(diff) / 10000, 2) if diff is not None else None,
            "diff_pct": round(float(cell["diff_pct"]), 1) if cell.get("diff_pct") is not None else None,
            "direction": "above" if diff is not None and diff > 0 else ("below" if diff is not None and diff < 0 else "same"),
        })
    old_cities.sort(key=lambda cell: (cell["median_yuan"] is None, cell["median_yuan"] or 0, cell["city"] or ""))
    def brief(cell):
        return {key: cell.get(key) for key in ("city", "median_wan", "diff_pct", "direction")}
    result.update({
        "series": series, "year": year, "min_city_n": min_city_n,
        "raw_count": int(region.get("baseline_n") or 0),
        "n_cities_included": int(region.get("valid_city_count") or 0),
        "national_median_yuan": baseline,
        "national_median_wan": round(float(baseline) / 10000, 2) if baseline is not None else None,
        "cities": old_cities, "cheapest_cities": [brief(cell) for cell in old_cities[:3]],
        "priciest_cities": [brief(cell) for cell in old_cities[-3:][::-1]],
        "confidence": "none", "note": "城市差异为共享分析口径；请查看 analysis result 的限制说明。",
    })
    return result
