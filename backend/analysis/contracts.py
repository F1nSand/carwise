from __future__ import annotations

import math
from typing import Any

ANALYSIS_TYPES = (
    "price_distribution",
    "mileage_price_correlation",
    "model_comparison",
    "brand_profile",
    "region_gap",
    "age_price_relation",
)
FILTER_KEYS = (
    "brands",
    "series_selectors",
    "cities",
    "price_min_yuan",
    "price_max_yuan",
    "mileage_min_km",
    "mileage_max_km",
    "year_min",
    "year_max",
    "age_min_years",
    "age_max_years",
    "energy_types",
)


class AnalysisInputError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if hasattr(value, "item"):
        try:
            value = value.item()
        except (ValueError, TypeError):
            pass
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def normalize_filters(filters: dict | None) -> dict:
    if filters is None:
        filters = {}
    if not isinstance(filters, dict):
        raise AnalysisInputError("invalid_filters", "filters must be an object")

    unknown = set(filters) - set(FILTER_KEYS)
    if unknown:
        raise AnalysisInputError(
            "unsupported_filter",
            f"Unsupported filters: {', '.join(sorted(unknown))}",
        )

    normalized = {key: [] for key in ("brands", "series_selectors", "cities", "energy_types")}
    normalized.update({key: None for key in FILTER_KEYS if key not in normalized})
    normalized.update(filters)

    limits = (("brands", 20), ("cities", 20), ("energy_types", 10), ("series_selectors", 6))
    for key, limit in limits:
        values = normalized[key]
        if values is None:
            values = []
        if not isinstance(values, list) or len(values) > limit:
            raise AnalysisInputError(
                "invalid_filters", f"{key} must be a list with at most {limit} items"
            )

        if key == "series_selectors":
            selectors = []
            for item in values:
                if (
                    not isinstance(item, dict)
                    or set(item) != {"brand_name", "series"}
                    or not all(isinstance(item[field], str) for field in ("brand_name", "series"))
                ):
                    raise AnalysisInputError(
                        "invalid_filters",
                        "series_selectors items require string brand_name and series",
                    )
                brand = item["brand_name"].strip()
                series = item["series"].strip()
                if not brand or not series:
                    raise AnalysisInputError("invalid_filters", "series identity cannot be blank")
                selectors.append({"brand_name": brand, "series": series})
            identities = {(item["brand_name"], item["series"]) for item in selectors}
            if len(identities) != len(selectors):
                raise AnalysisInputError("invalid_filters", "series_selectors cannot contain duplicates")
            normalized[key] = sorted(selectors, key=lambda item: (item["brand_name"], item["series"]))
            continue

        if any(not isinstance(item, str) for item in values):
            raise AnalysisInputError("invalid_filters", f"{key} items must be strings")
        cleaned = [item.strip() for item in values]
        if any(not item for item in cleaned):
            raise AnalysisInputError("invalid_filters", f"{key} items cannot be blank")
        normalized[key] = sorted(set(cleaned))

    numeric_keys = (
        "price_min_yuan",
        "price_max_yuan",
        "mileage_min_km",
        "mileage_max_km",
        "age_min_years",
        "age_max_years",
    )
    for key in numeric_keys:
        value = normalized[key]
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise AnalysisInputError("invalid_range", f"{key} must be a finite non-negative number")
        value = float(value)
        if not math.isfinite(value) or value < 0:
            raise AnalysisInputError("invalid_range", f"{key} must be a finite non-negative number")
        normalized[key] = value

    for key in ("year_min", "year_max"):
        value = normalized[key]
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
            raise AnalysisInputError("invalid_range", f"{key} must be an integer")

    ranges = (
        ("price_min_yuan", "price_max_yuan"),
        ("mileage_min_km", "mileage_max_km"),
        ("year_min", "year_max"),
        ("age_min_years", "age_max_years"),
    )
    for lower, upper in ranges:
        if (
            normalized[lower] is not None
            and normalized[upper] is not None
            and normalized[lower] > normalized[upper]
        ):
            raise AnalysisInputError("invalid_range", f"{lower} cannot exceed {upper}")
    return normalized
