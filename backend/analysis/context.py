from __future__ import annotations
from dataclasses import dataclass, field
import pandas as pd
from backend.data_loader import DatasetSnapshot, get_dataset_snapshot
from .filtering import apply_filters
from .contracts import normalize_filters

@dataclass
class AnalysisContext:
    snapshot: DatasetSnapshot
    frame: pd.DataFrame
    normalized_filters: dict
    filters_applied: tuple[str,...]
    dataset_rows: int
    scope_rows: int
    stage_counts: dict[str, int] = field(default_factory=dict)

def build_context(filters=None, snapshot: DatasetSnapshot|None=None) -> AnalysisContext:
    snap=snapshot or get_dataset_snapshot()
    normalized=normalize_filters(filters)
    # Copy only canonical analysis fields to avoid mutating or duplicating raw text columns.
    cols=[c for c in ("price_yuan","mileage_km","production_year","vehicle_age_years","city_name","brand_name","series","energy_type") if c in snap.dataframe.columns]
    projection=snap.dataframe.loc[:,cols]
    stage_counts = {}
    scoped, labels = apply_filters(projection, normalized, stage_counts=stage_counts)
    return AnalysisContext(
        snap, scoped, normalized, tuple(labels), len(snap.dataframe), len(scoped), stage_counts
    )
