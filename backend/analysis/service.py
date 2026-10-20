from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from backend.data_loader import get_dataset_snapshot
from .age_price import age_price_relation
from .brand_profile import brand_profile
from .comparison import model_comparison
from .context import build_context
from .contracts import AnalysisInputError, json_safe, normalize_filters
from .correlation import mileage_price_correlation
from .distribution import price_distribution
from .region import region_gap

_HANDLERS = {
    "price_distribution": price_distribution,
    "mileage_price_correlation": mileage_price_correlation,
    "model_comparison": model_comparison,
    "brand_profile": brand_profile,
    "region_gap": region_gap,
    "age_price_relation": age_price_relation,
}
_ALLOWED_PARAMS = {
    "price_distribution": {"bins"},
    "brand_profile": {"top_k"},
    "region_gap": {"min_city_n"},
    "age_price_relation": {"bin_width_years"},
}


def normalize_params(analysis_type: str, params: Any) -> dict:
    allowed = _ALLOWED_PARAMS.get(analysis_type, set())
    if not isinstance(params, dict) or set(params) - allowed:
        raise AnalysisInputError("invalid_params", "Unknown analysis params")
    return params


def run_analysis(
    analysis_type: str,
    filters=None,
    params=None,
    snapshot=None,
) -> dict[str, Any]:
    if not isinstance(analysis_type, str) or analysis_type not in _HANDLERS:
        raise AnalysisInputError("unknown_analysis_type", f"Unknown analysis_type: {analysis_type}")

    current_snapshot = snapshot or get_dataset_snapshot()
    normalized_filters = normalize_filters(filters)
    context = build_context(normalized_filters, current_snapshot)
    normalized_params = normalize_params(
        analysis_type,
        {} if params is None else params,
    )
    result = _HANDLERS[analysis_type](context, normalized_params)
    return json_safe(result)


def run_dashboard(filters=None, snapshot=None) -> dict[str, Any]:
    current_snapshot = snapshot or get_dataset_snapshot()
    normalized_filters = normalize_filters(filters)
    context = build_context(normalized_filters, current_snapshot)
    analyses = {}

    for analysis_type, handler in _HANDLERS.items():
        try:
            analyses[analysis_type] = json_safe(handler(context, {}))
        except Exception as exc:
            analyses[analysis_type] = {
                "schema_version": "1.0",
                "analysis_type": analysis_type,
                "status": "error",
                "reason_code": "analysis_failed",
                "dataset_version": current_snapshot.version,
                "normalized_filters": normalized_filters,
                "filters_applied": list(context.filters_applied),
                "counts": {
                    "dataset_rows": len(current_snapshot.dataframe),
                    "scope_rows": len(context.frame),
                    "valid_rows": 0,
                    "excluded_rows": 0,
                    "missing_required_rows": 0,
                    **context.stage_counts,
                },
                "data": {},
                "limitations": [str(exc)],
                "methodology": {},
            }

    statuses = {item["status"] for item in analyses.values()}
    if statuses == {"error"}:
        status = "error"
    elif "error" in statuses:
        status = "partial_success"
    elif context.scope_rows == 0 or statuses == {"empty"}:
        status = "empty"
    elif statuses == {"unsupported"}:
        status = "unsupported"
    else:
        status = "success"

    frame = context.frame
    overview = {
        "dataset_rows": len(current_snapshot.dataframe),
        "scope_rows": len(frame),
        "price_valid_rows": int(frame.price_yuan.notna().sum()) if "price_yuan" in frame else 0,
        "city_valid_rows": int(frame.city_name.notna().sum()) if "city_name" in frame else 0,
        "vehicle_age_valid_rows": (
            int(frame.vehicle_age_years.notna().sum())
            if "vehicle_age_years" in frame
            else 0
        ),
    }
    return json_safe(
        {
            "schema_version": "1.0",
            "status": status,
            "dataset_version": current_snapshot.version,
            "normalized_filters": normalized_filters,
            "overview": overview,
            "analyses": analyses,
        }
    )


def build_catalog(snapshot=None) -> dict[str, Any]:
    current_snapshot = snapshot or get_dataset_snapshot()
    frame = current_snapshot.dataframe

    def values(column: str) -> list[str]:
        if column not in frame:
            return []
        return sorted(
            {
                str(value).strip()
                for value in frame[column].dropna()
                if str(value).strip()
            }
        )

    units = {
        "price_yuan": "CNY",
        "mileage_km": "km",
        "production_year": "year",
        "vehicle_age_years": "year",
    }
    fields = {}
    for column in (
        "price_yuan",
        "mileage_km",
        "production_year",
        "vehicle_age_years",
        "city_name",
        "brand_name",
        "series",
        "energy_type",
    ):
        series = frame[column] if column in frame else None
        fields[column] = {
            "coverage": (
                float(series.notna().mean())
                if series is not None and len(series)
                else 0.0
            ),
            "unit": units.get(column),
            "supported_filter": bool(series is not None and series.notna().any()),
        }

    ranges = {}
    for column in ("price_yuan", "mileage_km", "production_year", "vehicle_age_years"):
        if column not in frame:
            continue
        numeric = pd.to_numeric(frame[column], errors="coerce")
        numeric = numeric.replace([np.inf, -np.inf], np.nan).dropna()
        if column == "price_yuan":
            numeric = numeric[(numeric > 0) & (numeric < 5_000_000)]
        elif column == "mileage_km":
            numeric = numeric[numeric < 600_000]
        elif column == "production_year":
            numeric = numeric[(numeric >= 1980) & (numeric <= 2027)]
        ranges[column] = {
            "min": float(numeric.min()) if len(numeric) else None,
            "max": float(numeric.max()) if len(numeric) else None,
        }

    selectors = []
    if "brand_name" in frame and "series" in frame:
        pairs = frame[["brand_name", "series"]].dropna()
        identities = sorted(
            {
                (str(brand), str(series))
                for brand, series in zip(pairs.brand_name, pairs.series)
                if str(series).strip()
            }
        )
        selectors = [
            {"brand_name": brand, "series": series}
            for brand, series in identities
        ]

    return {
        "dataset_version": current_snapshot.version,
        "brands": values("brand_name"),
        "series_selectors": selectors,
        "cities": values("city_name"),
        "energy_types": values("energy_type"),
        "fields": fields,
        "ranges": ranges,
        "defaults": {
            "filters": normalize_filters({}),
            "params": {
                "price_distribution": {"bins": 20},
                "brand_profile": {"top_k": 10},
                "region_gap": {"min_city_n": 15},
                "age_price_relation": {"bin_width_years": 1.0},
            },
        },
        "data_time_range": {"status": "unknown", "start": None, "end": None},
    }
