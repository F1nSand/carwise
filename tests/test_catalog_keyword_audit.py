import pandas as pd

from backend.data_loader import DatasetSnapshot
from scripts import audit_catalog_keywords as audit_script


def _snapshot():
    frame = pd.DataFrame([
        {"brand_name": "甲", "series": "HIACE", "city_name": "北京", "energy_type": "纯电动"},
        {"brand_name": "甲", "series": "HiAce", "city_name": "北京", "energy_type": "纯电动"},
        {"brand_name": "乙", "series": "HIACE", "city_name": "上海", "energy_type": "增程式"},
    ])
    return DatasetSnapshot("audit-fixture", frame, {}, [])


def test_audit_accepts_required_disambiguation_and_checks_every_identity():
    report = audit_script.audit(_snapshot())
    assert report["resolution_failures"] == []
    assert report["counts"]["series_identities"] == 3
    assert report["normalized_name_collisions"]
    assert any(check["input"] == "hiace" and check["status"] == "ambiguous_series"
               for check in report["checks"])
    assert any(check["input"] == "北京市" and check["passed"] for check in report["checks"])
    assert "纯电" in report["energy_aliases"]


def test_audit_detects_silent_selection_of_the_wrong_identity(monkeypatch):
    original = audit_script.resolve_keyword

    def wrong_resolver(snapshot, field, value, filters, tool):
        if field == "series" and value == "hiace":
            return {"brand_name": "甲", "series": "HIACE"}, None
        return original(snapshot, field, value, filters, tool)

    monkeypatch.setattr(audit_script, "resolve_keyword", wrong_resolver)
    report = audit_script.audit(_snapshot())
    assert report["counts"]["failures"] > 0
    assert any(failure["input"] == "hiace" for failure in report["resolution_failures"])
