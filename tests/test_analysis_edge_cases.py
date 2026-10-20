from types import SimpleNamespace

import pandas as pd
import pytest

from backend.analysis.age_price import age_price_relation
import backend.analysis.age_price as age_module
from backend.analysis.context import build_context
from backend.analysis.service import run_analysis, run_dashboard
import backend.analysis.service as analysis_service
from backend.analysis.statistics import numeric


def snapshot(frame):
    return SimpleNamespace(version="edges-v1", dataframe=frame, catalog={}, all_series=[])


def test_optional_numeric_fields_have_empty_series_fallback():
    assert numeric([]).empty
    frame = pd.DataFrame({"price_yuan": [10000, 20000], "brand_name": ["A", "B"],
                          "series": ["X", "Y"]})
    result = run_analysis("brand_profile", snapshot=snapshot(frame))
    assert result["status"] == "success"
    assert all(group["mileage"] == {"n": 0, "median_km": None} for group in result["data"]["groups"])


def test_global_exclusions_and_user_filter_exclusions_are_separate():
    frame = pd.DataFrame({"price_yuan": [10000, 20000, 5000000], "brand_name": ["A", "B", "A"]})
    result = run_analysis("price_distribution", {"brands": ["A"]}, snapshot=snapshot(frame))
    counts = result["counts"]
    assert counts["global_excluded_rows"] == 1
    assert counts["user_excluded_rows"] == 1
    assert counts["valid_rows"] == counts["scope_rows"] == 1
    assert counts["global_excluded_rows"] + counts["user_excluded_rows"] + counts["scope_rows"] == counts["dataset_rows"]


def test_dashboard_no_matching_scope_is_empty_even_without_comparison_selection():
    frame = pd.DataFrame({"price_yuan": [10000], "brand_name": ["A"], "series": ["X"],
                          "city_name": ["city"], "mileage_km": [1000], "vehicle_age_years": [2]})
    result = run_dashboard({"brands": ["does-not-exist"]}, snapshot(frame))
    assert result["overview"]["scope_rows"] == 0
    assert result["status"] == "empty"


def test_single_metric_failure_keeps_other_analysis_results(monkeypatch):
    frame = pd.DataFrame({"price_yuan": [10000], "brand_name": ["A"], "series": ["X"],
                          "city_name": ["city"], "mileage_km": [1000], "vehicle_age_years": [2]})
    def fail_metric(*args, **kwargs):
        raise RuntimeError("injected metric failure")
    monkeypatch.setitem(analysis_service._HANDLERS, "brand_profile", fail_metric)
    result = run_dashboard({}, snapshot(frame))
    assert result["status"] == "partial_success"
    assert result["analyses"]["brand_profile"]["status"] == "error"
    assert result["analyses"]["price_distribution"]["data"]["median_yuan"] == 10000
    assert result["analyses"]["age_price_relation"]["status"] == "success"


def test_age_bin_limit_is_checked_before_allocating_edges(monkeypatch):
    frame = pd.DataFrame({"price_yuan": [10000, 20000], "vehicle_age_years": [0, 10**12]})
    context = build_context({}, snapshot(frame))
    def reject_allocation(*args, **kwargs):
        raise AssertionError("Oversized edges were allocated before validating the bin count")
    monkeypatch.setattr(age_module.np, "arange", reject_allocation)
    with pytest.raises(ValueError, match="200 bins"):
        age_price_relation(context, {"bin_width_years": .25})
