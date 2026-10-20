import pandas as pd
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import threading
import pytest

from backend import data_loader
from backend.agent_runtime.tool_registry import tool_schemas
from backend.data_loader import ALL_SERIES, SERIES_CATALOG, DatasetChangedDuringReadError
from backend.tools import data_contract


def _write_dataset(path, *, price, series, brand, energy="纯电动"):
    pd.DataFrame({
        "price_yuan": [price],
        "car_name": [series],
        "brand_name": [brand],
        "energy_type": [energy],
        "mileage_km": [10_000],
        "production_year": [2022],
    }).to_parquet(path, index=False)


def _write_manifest(parquet_path):
    csv_path = parquet_path.with_name(f"{parquet_path.stem}.csv")
    csv_path.write_bytes(b"id\nfixture\n")
    csv_hash = hashlib.sha256(csv_path.read_bytes()).hexdigest()
    parquet_hash = hashlib.sha256(parquet_path.read_bytes()).hexdigest()
    manifest = {
        "format_version": 1,
        "batch_id": hashlib.sha256(f"{csv_hash}:{parquet_hash}".encode()).hexdigest(),
        "row_count": 1,
        "unique_id_count": 1,
        "artifacts": {
            csv_path.name: {"sha256": csv_hash, "rows": 1},
            parquet_path.name: {"sha256": parquet_hash, "rows": 1},
        },
    }
    parquet_path.with_name(f"{parquet_path.stem}.manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8")


def _reset_caches():
    with data_loader._snapshot_lock:
        data_loader._snapshot_cache.clear()
        data_loader._signature_cache.clear()
        data_loader._snapshot_inflight.clear()
        data_loader._snapshot_cache_bytes = 0
    data_loader._dataset_version_for_stat.cache_clear()
    data_contract._get_data_capabilities.cache_clear()
    data_contract._snapshot_cache.clear()


def test_dataframe_catalog_capabilities_and_tool_schema_share_replaced_snapshot(tmp_path, monkeypatch):
    source = tmp_path / "cars.parquet"
    _write_dataset(source, price=100, series="Alpha A", brand="Alpha")
    monkeypatch.setattr(data_loader, "PARQUET_PATH", source)
    _reset_caches()

    old_snapshot = data_loader.get_dataset_snapshot()
    old_capabilities = data_contract.get_data_capabilities(old_snapshot)
    assert old_snapshot.dataframe["price_yuan"].tolist() == [100]
    assert old_capabilities["dataset_version"] == old_snapshot.version
    assert data_loader.get_catalog() == {"Alpha": ["Alpha A"]}
    assert dict(SERIES_CATALOG) == {"Alpha": ["Alpha A"]}
    assert list(ALL_SERIES) == ["Alpha A"]

    replacement = tmp_path / "replacement.parquet"
    _write_dataset(replacement, price=200, series="Beta B", brand="Beta", energy="增程式")
    replacement.replace(source)

    new_snapshot = data_loader.get_dataset_snapshot()
    new_capabilities = data_contract.get_data_capabilities(new_snapshot)
    recommend_schema = next(
        schema for schema in tool_schemas()
        if schema["function"]["name"] == "recommend"
    )
    description = recommend_schema["function"]["description"]

    assert new_snapshot.version != old_snapshot.version
    assert data_loader.get_dataset_version() == new_snapshot.version
    assert data_loader.get_df()["price_yuan"].tolist() == [200]
    assert data_loader.get_catalog() == {"Beta": ["Beta B"]}
    assert dict(SERIES_CATALOG) == {"Beta": ["Beta B"]}
    assert list(ALL_SERIES) == ["Beta B"]
    assert new_capabilities["dataset_version"] == new_snapshot.version
    assert new_capabilities["row_count"] == len(new_snapshot.dataframe) == 1
    assert new_capabilities["energy_values"] == ["增程式"]
    assert new_snapshot.dataframe["series"].tolist() == new_snapshot.all_series
    assert new_snapshot.version in description
    assert "1 条记录" in description


def test_replacement_during_dataframe_build_retries_before_returning_snapshot(tmp_path, monkeypatch):
    source = tmp_path / "cars.parquet"
    replacement = tmp_path / "replacement.parquet"
    _write_dataset(source, price=100, series="Alpha A", brand="Alpha")
    _write_dataset(replacement, price=200, series="Beta B", brand="Beta")
    monkeypatch.setattr(data_loader, "PARQUET_PATH", source)
    _reset_caches()

    original_build = data_loader._dataframe_from_payload
    swapped = False

    def swap_after_read(payload):
        nonlocal swapped
        dataframe = original_build(payload)
        if not swapped:
            swapped = True
            replacement.replace(source)
        return dataframe

    monkeypatch.setattr(data_loader, "_dataframe_from_payload", swap_after_read)
    snapshot = data_loader.get_dataset_snapshot()

    assert swapped is True
    assert snapshot.dataframe["price_yuan"].tolist() == [200]
    assert snapshot.dataframe["series"].tolist() == ["Beta B"]
    assert snapshot.catalog == {"Beta": ["Beta B"]}
    assert snapshot.version == data_loader.get_dataset_version()


def test_concurrent_cold_load_uses_one_dataframe_build(tmp_path, monkeypatch):
    source = tmp_path / "cars.parquet"
    _write_dataset(source, price=100, series="Alpha A", brand="Alpha")
    monkeypatch.setattr(data_loader, "PARQUET_PATH", source)
    _reset_caches()
    original_build = data_loader._dataframe_from_payload
    entered = threading.Event()
    release = threading.Event()
    build_count = 0
    count_lock = threading.Lock()

    def counted_build(payload):
        nonlocal build_count
        with count_lock:
            build_count += 1
        entered.set()
        assert release.wait(5)
        return original_build(payload)

    monkeypatch.setattr(data_loader, "_dataframe_from_payload", counted_build)
    start = threading.Barrier(4)

    def load():
        start.wait(timeout=5)
        return data_loader.get_dataset_snapshot()

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(load) for _ in range(4)]
        assert entered.wait(5)
        release.set()
        snapshots = [future.result(timeout=5) for future in futures]

    assert build_count == 1
    assert all(snapshot is snapshots[0] for snapshot in snapshots)


def test_snapshot_cache_respects_dataframe_byte_budget(tmp_path, monkeypatch):
    source = tmp_path / "cars.parquet"
    _write_dataset(source, price=100, series="Alpha A", brand="Alpha")
    monkeypatch.setattr(data_loader, "PARQUET_PATH", source)
    monkeypatch.setattr(data_loader, "_MAX_SNAPSHOT_CACHE_BYTES", 0)
    _reset_caches()

    snapshot = data_loader.get_dataset_snapshot()

    assert snapshot.dataframe.iloc[0]["price_yuan"] == 100
    assert not data_loader._snapshot_cache
    assert data_loader._snapshot_cache_bytes == 0


def test_loader_accepts_only_parquet_matching_present_batch_manifest(tmp_path, monkeypatch):
    source = tmp_path / "cars.parquet"
    _write_dataset(source, price=100, series="Alpha A", brand="Alpha")
    _write_manifest(source)
    monkeypatch.setattr(data_loader, "PARQUET_PATH", source)
    _reset_caches()

    accepted = data_loader.get_dataset_snapshot()
    assert accepted.dataframe["price_yuan"].tolist() == [100]

    replacement = tmp_path / "replacement.parquet"
    _write_dataset(replacement, price=200, series="Beta B", brand="Beta")
    replacement.replace(source)
    with pytest.raises(DatasetChangedDuringReadError, match="快照构建"):
        data_loader.get_dataset_snapshot()

    _write_manifest(source)
    _reset_caches()
    published = data_loader.get_dataset_snapshot()
    assert published.dataframe["price_yuan"].tolist() == [200]


def test_manifest_may_finish_publishing_during_snapshot_retry(tmp_path, monkeypatch):
    source = tmp_path / "cars.parquet"
    _write_dataset(source, price=100, series="Alpha A", brand="Alpha")
    _write_manifest(source)
    monkeypatch.setattr(data_loader, "PARQUET_PATH", source)
    _reset_caches()
    replacement = tmp_path / "replacement.parquet"
    _write_dataset(replacement, price=200, series="Beta B", brand="Beta")
    replacement.replace(source)

    original_matches = data_loader._manifest_matches
    updated = False
    calls = 0

    def finish_manifest(path, version):
        nonlocal updated, calls
        calls += 1
        if calls == 1:
            return False
        if not updated:
            updated = True
            _write_manifest(path)
        return original_matches(path, version)

    monkeypatch.setattr(data_loader, "_manifest_matches", finish_manifest)
    snapshot = data_loader.get_dataset_snapshot()

    assert updated
    assert calls == 2
    assert snapshot.dataframe["price_yuan"].tolist() == [200]


def test_cached_version_cannot_bypass_mismatched_or_malformed_manifest(tmp_path, monkeypatch):
    source = tmp_path / "cars.parquet"
    _write_dataset(source, price=100, series="Alpha A", brand="Alpha")
    _write_manifest(source)
    monkeypatch.setattr(data_loader, "PARQUET_PATH", source)
    _reset_caches()

    accepted = data_loader.get_dataset_snapshot()
    assert data_loader.get_dataset_version() == accepted.version

    manifest_path = source.with_name("cars.manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["artifacts"][source.name] = "not-an-artifact-object"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(DatasetChangedDuringReadError, match="manifest"):
        data_loader.get_dataset_version()
    with pytest.raises(DatasetChangedDuringReadError, match="快照构建"):
        data_loader.get_dataset_snapshot()
