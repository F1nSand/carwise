import pandas as pd

from backend.tools import data_contract


def test_capability_summary_reports_supported_fields_unknown_collection_time_and_version(monkeypatch):
    monkeypatch.setattr(data_contract, "get_dataset_version", lambda: "fixture-v1")
    monkeypatch.setattr(data_contract, "get_df", lambda: pd.DataFrame({
        "price_yuan": [10000.0, None],
        "energy_type": ["纯电动", None],
    }))
    data_contract._get_data_capabilities.cache_clear()

    summary = data_contract.get_data_capabilities()

    assert summary["dataset_version"] == "fixture-v1"
    assert summary["fields"]["price_yuan"]["coverage"] == 0.5
    assert summary["fields"]["body_type"]["coverage"] == 0.0
    assert summary["energy_values"] == ["纯电动"]
    assert summary["energy_aliases"]["纯电"] == ["纯电动"]
    assert "body_type" in summary["unsupported_filters"]
    assert summary["data_time_range"] == {"status": "unknown", "start": None, "end": None}
    assert data_contract.contract_summary()["dataset_version"] == "fixture-v1"
    assert data_contract.contract_summary()["energy_aliases"]["纯电"] == ["纯电动"]


def test_capability_cache_refreshes_when_dataset_version_changes(monkeypatch):
    version = ["v1"]
    monkeypatch.setattr(data_contract, "get_dataset_version", lambda: version[0])
    monkeypatch.setattr(data_contract, "get_df", lambda: pd.DataFrame({"energy_type": [version[0]]}))
    data_contract._get_data_capabilities.cache_clear()

    first = data_contract.get_data_capabilities()
    version[0] = "v2"
    second = data_contract.get_data_capabilities()

    assert first["dataset_version"] == "v1"
    assert second["dataset_version"] == "v2"
    assert second["energy_values"] == ["v2"]
