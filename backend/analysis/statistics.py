from __future__ import annotations

import numpy as np
import pandas as pd


def numeric(series: pd.Series) -> pd.Series:
    if not isinstance(series, pd.Series):
        series = pd.Series(series)
    return (
        pd.to_numeric(series, errors="coerce")
        .replace([np.inf, -np.inf], np.nan)
        .dropna()
    )


def quantiles(series: pd.Series) -> dict:
    values = numeric(series)
    if values.empty:
        return {
            "n": 0,
            "min": None,
            "max": None,
            "mean": None,
            "median": None,
            "p25": None,
            "p75": None,
        }
    return {
        "n": int(values.size),
        "min": float(values.min()),
        "max": float(values.max()),
        "mean": float(values.mean()),
        "median": float(values.median()),
        "p25": float(values.quantile(0.25, interpolation="linear")),
        "p75": float(values.quantile(0.75, interpolation="linear")),
    }


def make_result(
    context,
    analysis_type: str,
    data: dict,
    valid: int,
    excluded: int = 0,
    limitations=None,
    methodology=None,
    reason=None,
    status=None,
    missing=None,
) -> dict:
    result_status = status or ("success" if valid else "empty")
    return {
        "schema_version": "1.0",
        "analysis_type": analysis_type,
        "status": result_status,
        "reason_code": reason,
        "dataset_version": context.snapshot.version,
        "normalized_filters": context.normalized_filters,
        "filters_applied": list(context.filters_applied),
        "counts": {
            "dataset_rows": context.dataset_rows,
            "scope_rows": context.scope_rows,
            "valid_rows": int(valid),
            "excluded_rows": int(excluded),
            "missing_required_rows": int(excluded if missing is None else missing),
            **context.stage_counts,
        },
        "data": data,
        "limitations": limitations or [],
        "methodology": methodology or {},
    }
