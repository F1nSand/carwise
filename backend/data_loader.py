# -*- coding: utf-8 -*-
"""Parquet 数据快照加载与车系规范化。"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import threading
from concurrent.futures import Future
from collections import OrderedDict
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import pandas as pd

from backend.config import PARQUET_PATH

_PAREN_RE = re.compile(r"[（(][^（()）]*[)）]")
_WS_RE = re.compile(r"\s+")
_MAX_SNAPSHOTS = 3
try:
    _MAX_SNAPSHOT_CACHE_BYTES = max(0, int(os.environ.get("DATASET_SNAPSHOT_CACHE_MAX_BYTES", 512 * 1024 * 1024)))
except ValueError:
    _MAX_SNAPSHOT_CACHE_BYTES = 512 * 1024 * 1024
_MAX_READ_ATTEMPTS = 3
_snapshot_lock = threading.RLock()
_snapshot_cache: OrderedDict[str, "DatasetSnapshot"] = OrderedDict()
_signature_cache: OrderedDict[tuple[Any, ...], str] = OrderedDict()
_snapshot_cache_bytes = 0
_snapshot_inflight: dict[tuple[Any, ...], Future] = {}


class DatasetChangedDuringReadError(RuntimeError):
    """The parquet path changed while a stable snapshot was being built."""


def normalize_series(car_name: str) -> str:
    """把完整车型名规范成车系键。"""
    if not isinstance(car_name, str):
        return ""
    return _WS_RE.sub(" ", _PAREN_RE.sub("", car_name)).strip()


def _file_signature(path: Path) -> tuple[int, int, int, int]:
    stat = path.stat()
    # Windows reports creation time through st_ctime while fstat may expose
    # filesystem change time, so comparing ctime across these APIs is invalid.
    return (int(stat.st_dev), int(stat.st_ino), int(stat.st_size), int(stat.st_mtime_ns))


def _signature_key(path: Path, signature: tuple[int, int, int, int]) -> tuple[Any, ...]:
    return (str(path.resolve()), *signature)


def _read_stable_payload(path: Path | None = None) -> tuple[bytes, str, tuple[int, int, int, int]]:
    """Read one immutable byte payload, retrying if the path changes mid-read."""
    source_path = Path(path or PARQUET_PATH)
    if not source_path.exists():
        raise FileNotFoundError(f"未找到清洗数据文件：{source_path}")
    for _ in range(_MAX_READ_ATTEMPTS):
        try:
            before = _file_signature(source_path)
            with source_path.open("rb") as source:
                opened_before = _signature_from_stat(source.fileno())
                payload = source.read()
                opened_after = _signature_from_stat(source.fileno())
            after = _file_signature(source_path)
        except FileNotFoundError:
            continue
        if before != opened_before or opened_before != opened_after or opened_after != after:
            continue
        return payload, hashlib.sha256(payload).hexdigest(), after
    raise DatasetChangedDuringReadError(
        f"数据文件在连续 {_MAX_READ_ATTEMPTS} 次读取期间发生变化：{source_path}"
    )


def _signature_from_stat(file_descriptor: int) -> tuple[int, int, int, int]:
    import os

    stat = os.fstat(file_descriptor)
    return (int(stat.st_dev), int(stat.st_ino), int(stat.st_size), int(stat.st_mtime_ns))


@dataclass(frozen=True)
class DatasetSnapshot:
    """DataFrame and derived catalog from the exact same parquet byte identity."""

    version: str
    dataframe: pd.DataFrame
    catalog: dict[str, list[str]]
    all_series: list[str]


def _dataframe_from_payload(payload: bytes) -> pd.DataFrame:
    df = pd.read_parquet(io.BytesIO(payload))
    if "car_name" in df.columns:
        df["series"] = df["car_name"].map(normalize_series)
    else:
        df["series"] = ""
    for column in ("price_yuan", "mileage_km", "production_year"):
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors="coerce")
    return df


def _manifest_matches(path: Path, version: str) -> bool:
    """Accept legacy datasets without a manifest; reject an uncommitted batch when present."""
    manifest_path = path.with_name(f"{path.stem}.manifest.json")
    if not manifest_path.exists():
        return True
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or not isinstance(manifest.get("artifacts"), dict):
            return False
        artifact = manifest["artifacts"].get(path.name)
        csv_artifact = manifest["artifacts"].get(f"{path.stem}.csv")
    except (OSError, ValueError, KeyError, TypeError):
        return False
    if not isinstance(artifact, dict) or not isinstance(csv_artifact, dict):
        return False
    return (
        manifest.get("format_version") == 1
        and isinstance(manifest.get("batch_id"), str)
        and len(manifest["batch_id"]) == 64
        and artifact.get("sha256") == version
        and isinstance(artifact.get("rows"), int)
        and artifact["rows"] == manifest.get("row_count")
        and csv_artifact.get("rows") == manifest.get("row_count")
        and manifest.get("unique_id_count") == manifest.get("row_count")
        and manifest.get("batch_id") == hashlib.sha256(
            f"{csv_artifact.get('sha256')}:{artifact.get('sha256')}".encode()
        ).hexdigest()
    )


def _catalog_from_dataframe(df: pd.DataFrame) -> tuple[dict[str, list[str]], list[str]]:
    brand_col = "brand_name" if "brand_name" in df.columns else "品牌名称"
    if brand_col not in df.columns or "series" not in df.columns:
        return {}, []
    sub = df[[brand_col, "series"]].dropna()
    catalog: dict[str, list[str]] = {}
    for brand, group in sub.groupby(brand_col):
        series_list = sorted(set(group["series"].astype(str)) - {""})
        if series_list:
            catalog[str(brand)] = series_list
    all_series = sorted({series for values in catalog.values() for series in values}, key=len, reverse=True)
    return catalog, all_series


def get_dataset_snapshot() -> DatasetSnapshot:
    """Return one coherent, content-addressed dataframe/catalog snapshot."""
    global _snapshot_cache_bytes
    source_path = Path(PARQUET_PATH)
    for _ in range(_MAX_READ_ATTEMPTS):
        try:
            observed_signature = _file_signature(source_path)
        except FileNotFoundError:
            raise FileNotFoundError(f"未找到清洗数据文件：{source_path}")
        signature_key = _signature_key(source_path, observed_signature)
        with _snapshot_lock:
            cached_version = _signature_cache.get(signature_key)
            cached = _snapshot_cache.get(cached_version) if cached_version else None
            if cached is not None and _manifest_matches(source_path, cached.version):
                _snapshot_cache.move_to_end(cached.version)
                _signature_cache.move_to_end(signature_key)
                return cached
            pending = _snapshot_inflight.get(signature_key)
            if pending is None:
                pending = Future()
                _snapshot_inflight[signature_key] = pending
                is_loader = True
            else:
                is_loader = False
        if not is_loader:
            return pending.result()
        try:
            result = _load_dataset_snapshot(source_path)
        except BaseException as exc:
            with _snapshot_lock:
                _snapshot_inflight.pop(signature_key, None)
                pending.set_exception(exc)
            raise
        else:
            with _snapshot_lock:
                _snapshot_inflight.pop(signature_key, None)
                pending.set_result(result)
            return result
    raise DatasetChangedDuringReadError(
        f"数据文件在连续 {_MAX_READ_ATTEMPTS} 次快照构建期间发生变化：{PARQUET_PATH}"
    )


def _load_dataset_snapshot(source_path: Path) -> DatasetSnapshot:
    global _snapshot_cache_bytes
    for _ in range(_MAX_READ_ATTEMPTS):
        payload, version, signature = _read_stable_payload()
        signature_key = _signature_key(source_path, signature)
        try:
            if _file_signature(source_path) != signature:
                continue
        except FileNotFoundError:
            continue
        if not _manifest_matches(source_path, version):
            continue
        try:
            if _file_signature(source_path) != signature:
                continue
        except FileNotFoundError:
            continue
        with _snapshot_lock:
            cached = _snapshot_cache.get(version)
            if cached is not None:
                _snapshot_cache.move_to_end(version)
                return cached
        dataframe = _dataframe_from_payload(payload)
        catalog, all_series = _catalog_from_dataframe(dataframe)
        try:
            if _file_signature(Path(PARQUET_PATH)) != signature:
                continue
        except FileNotFoundError:
            continue
        snapshot = DatasetSnapshot(version, dataframe, catalog, all_series)
        with _snapshot_lock:
            existing = _snapshot_cache.get(version)
            if existing is not None:
                _snapshot_cache.move_to_end(version)
                return existing
            size_bytes = int(dataframe.memory_usage(index=True, deep=True).sum())
            if size_bytes <= _MAX_SNAPSHOT_CACHE_BYTES:
                _snapshot_cache[version] = snapshot
                _snapshot_cache_bytes += size_bytes
                while len(_snapshot_cache) > _MAX_SNAPSHOTS or _snapshot_cache_bytes > _MAX_SNAPSHOT_CACHE_BYTES:
                    _, evicted = _snapshot_cache.popitem(last=False)
                    _snapshot_cache_bytes -= int(evicted.dataframe.memory_usage(index=True, deep=True).sum())
            _signature_cache[signature_key] = version
            while len(_signature_cache) > 8:
                _signature_cache.popitem(last=False)
        return snapshot
    raise DatasetChangedDuringReadError(
        f"数据文件在连续 {_MAX_READ_ATTEMPTS} 次快照构建期间发生变化：{source_path}"
    )


def get_dataset_version() -> str:
    """Stable SHA-256 fingerprint of a consistent parquet byte payload."""
    source_path = Path(PARQUET_PATH)
    signature = _file_signature(source_path)
    key = _signature_key(source_path, signature)
    with _snapshot_lock:
        cached = _signature_cache.get(key)
    if cached:
        if _manifest_matches(source_path, cached):
            with _snapshot_lock:
                _signature_cache.move_to_end(key)
            return cached
        raise DatasetChangedDuringReadError(
            f"数据文件的批次 manifest 与已缓存版本不匹配：{source_path}"
        )
    for _ in range(_MAX_READ_ATTEMPTS):
        payload, version, verified_signature = _read_stable_payload(source_path)
        try:
            if _file_signature(source_path) != verified_signature:
                continue
        except FileNotFoundError:
            continue
        if not _manifest_matches(source_path, version):
            continue
        try:
            if _file_signature(source_path) != verified_signature:
                continue
        except FileNotFoundError:
            continue
        key = _signature_key(source_path, verified_signature)
        with _snapshot_lock:
            _signature_cache[key] = version
            while len(_signature_cache) > 8:
                _signature_cache.popitem(last=False)
        return version
    raise DatasetChangedDuringReadError(
        f"数据文件在连续 {_MAX_READ_ATTEMPTS} 次版本核验期间未找到匹配 manifest：{source_path}"
    )


@lru_cache(maxsize=4)
def _dataset_version_for_stat(path: str, size: int, mtime_ns: int) -> str:
    """Compatibility helper retained for callers that explicitly clear this cache."""
    return _read_stable_payload(Path(path))[1]


def get_df() -> pd.DataFrame:
    """Return the DataFrame belonging to the current immutable file snapshot."""
    return get_dataset_snapshot().dataframe


def get_catalog() -> dict[str, list[str]]:
    """Return an isolated catalog from the current data snapshot."""
    snapshot = get_dataset_snapshot()
    return {brand: list(series) for brand, series in snapshot.catalog.items()}


def get_all_series() -> list[str]:
    """Return all series from the current data snapshot, longest first."""
    return list(get_dataset_snapshot().all_series)


class _CatalogProxy(Mapping[str, list[str]]):
    """Backward-compatible module constant that always resolves current data."""

    def __getitem__(self, key: str) -> list[str]:
        return get_catalog()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(get_catalog())

    def __len__(self) -> int:
        return len(get_catalog())

    def __repr__(self) -> str:
        return repr(get_catalog())

    def __eq__(self, other: object) -> bool:
        return get_catalog() == other


class _SeriesProxy(Sequence[str]):
    """Backward-compatible sequence that does not pin a stale catalog."""

    def __getitem__(self, index: Any) -> Any:
        return get_all_series()[index]

    def __len__(self) -> int:
        return len(get_all_series())

    def __iter__(self) -> Iterator[str]:
        return iter(get_all_series())

    def __repr__(self) -> str:
        return repr(get_all_series())

    def __eq__(self, other: object) -> bool:
        return get_all_series() == other


SERIES_CATALOG: Mapping[str, list[str]] = _CatalogProxy()
ALL_SERIES: Sequence[str] = _SeriesProxy()
