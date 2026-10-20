"""数据字段能力与筛选口径，和 parquet DataFrame 共用内容快照。"""
from __future__ import annotations

from functools import lru_cache
import threading
from typing import Any

import pandas as pd

from backend.data_loader import DatasetSnapshot, get_dataset_snapshot, get_dataset_version, get_df

ENERGY_ALIASES: dict[str, tuple[str, ...]] = {
    "纯电": ("纯电动",),
    "纯电动": ("纯电动",),
    "插混": ("插电式混合动力",),
    "插电混合": ("插电式混合动力",),
    "插电式混合动力": ("插电式混合动力",),
    "增程": ("增程式",),
    "增程式": ("增程式",),
    "混动": ("插电式混合动力", "增程式"),
    "新能源": ("纯电动", "插电式混合动力", "增程式"),
}
_REAL_GET_DATASET_VERSION = get_dataset_version
_REAL_GET_DF = get_df
_snapshot_cache: dict[str, dict[str, Any]] = {}
_cache_lock = threading.RLock()


def _coverage(df: pd.DataFrame, field: str) -> float:
    if field not in df.columns or len(df) == 0:
        return 0.0
    return round(float(df[field].notna().mean()), 4)


def _build_data_capabilities(version: str, df: pd.DataFrame) -> dict[str, Any]:
    energy_values = []
    if "energy_type" in df.columns:
        energy_values = sorted({str(value).strip() for value in df["energy_type"].dropna() if str(value).strip()})
    actual_energy_values = set(energy_values)
    energy_aliases = {
        alias: [value for value in candidates if value in actual_energy_values]
        for alias, candidates in ENERGY_ALIASES.items()
        if any(value in actual_energy_values for value in candidates)
    }
    fields = {
        field: {"coverage": _coverage(df, field)}
        for field in (
            "price_yuan", "mileage_km", "production_year", "vehicle_age_years",
            "city_name", "brand_name", "series", "energy_type", "body_type", "drive_type",
        )
    }
    return {
        "dataset_version": version,
        "row_count": int(len(df)),
        "fields": fields,
        "energy_values": energy_values,
        "energy_aliases": energy_aliases,
        "supported_filters": ["budget", "city", "brand", "series", "year", "energy_type", "max_age"],
        "unsupported_filters": ["body_type", "drive_type"],
        "data_time_range": {"status": "unknown", "start": None, "end": None},
    }


@lru_cache(maxsize=4)
def _get_data_capabilities(version: str) -> dict[str, Any]:
    """Legacy cache seam; production requests use an explicit coherent snapshot."""
    return _build_data_capabilities(version, get_df())


def get_data_capabilities(snapshot: DatasetSnapshot | None = None) -> dict[str, Any]:
    """Return field coverage for the exact DataFrame identified by dataset_version."""
    # Preserve the existing isolated-test injection seam. In production this
    # branch is never used: version and dataframe always come from one snapshot.
    if snapshot is None and (get_dataset_version is not _REAL_GET_DATASET_VERSION or get_df is not _REAL_GET_DF):
        return _get_data_capabilities(get_dataset_version())
    coherent = snapshot or get_dataset_snapshot()
    with _cache_lock:
        cached = _snapshot_cache.get(coherent.version)
        if cached is not None:
            return cached
    capabilities = _build_data_capabilities(coherent.version, coherent.dataframe)
    with _cache_lock:
        _snapshot_cache[coherent.version] = capabilities
        while len(_snapshot_cache) > 4:
            _snapshot_cache.pop(next(iter(_snapshot_cache)))
    return capabilities


def normalize_energy_filter(value: str | None, snapshot: DatasetSnapshot | None = None) -> tuple[list[str], str | None]:
    """将能源别名映射到指定数据快照中的真实枚举值。"""
    if not value:
        return [], None
    requested = str(value).strip()
    capabilities = get_data_capabilities(snapshot)
    actual = set(capabilities["energy_values"])
    candidates = ENERGY_ALIASES.get(requested, (requested,))
    resolved = [item for item in candidates if item in actual]
    if resolved:
        return resolved, None
    if requested in {"燃油", "汽油", "柴油", "油车"}:
        return [], "当前数据的能源字段缺失率较高，且没有可靠的燃油枚举，不能把缺失值当作燃油车筛选。"
    supported = "、".join(capabilities["energy_values"]) or "暂无可用能源枚举"
    return [], f"能源条件“{requested}”无法映射到真实数据。当前可识别值：{supported}."


def contract_summary(snapshot: DatasetSnapshot | None = None) -> dict[str, Any]:
    """给工具结果附加精简能力摘要。"""
    capabilities = get_data_capabilities(snapshot)
    return {
        "dataset_version": capabilities["dataset_version"],
        "row_count": capabilities["row_count"],
        "energy_values": capabilities["energy_values"],
        "energy_aliases": capabilities["energy_aliases"],
        "field_coverage": capabilities["fields"],
        "unsupported_filters": capabilities["unsupported_filters"],
        "data_time_range": capabilities["data_time_range"],
    }
