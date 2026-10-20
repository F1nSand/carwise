from __future__ import annotations

import pandas as pd

from backend.config import TH
from .contracts import AnalysisInputError, normalize_filters

COLUMNS = (
    "price_yuan",
    "mileage_km",
    "production_year",
    "vehicle_age_years",
    "city_name",
    "brand_name",
    "series",
    "energy_type",
)
ENERGY_ALIASES = {
    "纯电": ("纯电动",),
    "插混": ("插电式混合动力",),
    "插电混合": ("插电式混合动力",),
    "增程": ("增程式",),
    "混动": ("插电式混合动力", "增程式"),
    "新能源": ("纯电动", "插电式混合动力", "增程式"),
}


def _apply_global_filters(frame: pd.DataFrame) -> pd.DataFrame:
    mask = pd.Series(True, index=frame.index)
    if "price_yuan" in frame:
        mask &= frame.price_yuan.gt(TH["price_min_yuan"])
        mask &= frame.price_yuan.lt(TH["price_max_yuan"])
    if "mileage_km" in frame:
        mask &= frame.mileage_km.isna() | frame.mileage_km.lt(TH["mileage_max_km"])
    if "production_year" in frame:
        mask &= frame.production_year.isna() | (
            frame.production_year.ge(TH["year_min"])
            & frame.production_year.le(TH["year_max"])
        )
    return frame.loc[mask].copy()


def _select_values(frame: pd.DataFrame, field: str, values: list[str]) -> pd.DataFrame:
    if not values:
        return frame
    if field not in frame:
        raise AnalysisInputError("unsupported_filter", f"Field {field} is unavailable")
    return frame[frame[field].astype(str).isin(values)]


def _select_energy(
    frame: pd.DataFrame, values: list[str], *, available_values: set[str] | None = None
) -> pd.DataFrame:
    if not values:
        return frame
    if "energy_type" not in frame:
        raise AnalysisInputError("unsupported_filter", "Field energy_type is unavailable")
    actual = available_values if available_values is not None else set(frame.energy_type.dropna().astype(str))
    resolved = []
    for requested in values:
        candidates = ENERGY_ALIASES.get(requested, (requested,))
        matches = [value for value in candidates if value in actual]
        if not matches:
            raise AnalysisInputError("unsupported_filter", f"Unsupported energy type: {requested}")
        resolved.extend(matches)
    return frame[frame.energy_type.astype(str).isin(set(resolved))]


def apply_filters(frame: pd.DataFrame, filters: dict, *, stage_counts=None) -> tuple[pd.DataFrame, list[str]]:
    normalized = normalize_filters(filters)
    if (
        "vehicle_age_years" not in frame
        and (normalized["age_min_years"] is not None or normalized["age_max_years"] is not None)
    ):
        raise AnalysisInputError("unsupported_filter", "Unavailable field: vehicle_age_years")
    if normalized["energy_types"] and "energy_type" not in frame:
        raise AnalysisInputError("unsupported_filter", "Unavailable field: energy_type")

    # Validate requested energy against the full snapshot, before user filters
    # narrow the rows. A valid energy with no brand overlap is an empty result.
    available_energy = (
        set(frame.energy_type.dropna().astype(str))
        if normalized["energy_types"] and "energy_type" in frame else None
    )
    selected = _apply_global_filters(frame)
    global_rows = len(selected)
    selected = _select_values(selected, "brand_name", normalized["brands"])
    selected = _select_energy(
        selected, normalized["energy_types"], available_values=available_energy
    )

    if normalized["cities"]:
        if "city_name" not in selected:
            raise AnalysisInputError("unsupported_filter", "Field city_name is unavailable")
        cities = selected.city_name.fillna("").astype(str).str.strip()
        selected = selected[cities.isin(normalized["cities"])]

    if normalized["series_selectors"]:
        if "brand_name" not in selected or "series" not in selected:
            raise AnalysisInputError("unsupported_filter", "Series fields unavailable")
        choices = {
            (item["brand_name"], item["series"])
            for item in normalized["series_selectors"]
        }
        if normalized["brands"] and any(
            brand not in normalized["brands"] for brand, _ in choices
        ):
            raise AnalysisInputError("incompatible_filters", "Brand and series selections conflict")
        mask = pd.Series(
            [(str(brand), str(series)) in choices
             for brand, series in zip(selected.brand_name, selected.series)],
            index=selected.index,
            dtype=bool,
        )
        selected = selected.loc[mask]

    ranges = (
        ("price_yuan", "price_min_yuan", "price_max_yuan"),
        ("mileage_km", "mileage_min_km", "mileage_max_km"),
        ("production_year", "year_min", "year_max"),
        ("vehicle_age_years", "age_min_years", "age_max_years"),
    )
    for column, lower, upper in ranges:
        if normalized[lower] is None and normalized[upper] is None:
            continue
        if column not in selected:
            raise AnalysisInputError("unsupported_filter", f"Field {column} is unavailable")
        selected = selected[selected[column].notna()]
        if normalized[lower] is not None:
            selected = selected[selected[column] >= normalized[lower]]
        if normalized[upper] is not None:
            selected = selected[selected[column] <= normalized[upper]]

    labels = [
        f"{key}={value}"
        for key, value in normalized.items()
        if value not in (None, [])
    ]
    if stage_counts is not None:
        stage_counts.update(
            global_valid_rows=global_rows,
            global_excluded_rows=len(frame) - global_rows,
            user_excluded_rows=global_rows - len(selected),
        )
    return selected, labels
