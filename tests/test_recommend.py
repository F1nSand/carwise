# -*- coding: utf-8 -*-
"""
购车推荐工具的单元测试

运行：
  python -m pytest tests/test_recommend.py -q
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.tools.recommend import recommend_cars   # noqa: E402
from backend.tools.data_contract import get_data_capabilities, normalize_energy_filter  # noqa: E402
import backend.tools.recommend as recommend_module


class TestRecommend:
    def test_shape(self):
        r = recommend_cars(15)
        assert r["ok"] is True
        assert r["tool"] == "recommend"
        assert r["n_candidates"] > 0
        assert len(r["recommendations"]) > 0
        for key in ("budget_max_wan", "n_candidates", "weights", "recommendations",
                    "confidence"):
            assert key in r

    def test_median_within_budget(self):
        r = recommend_cars(15)
        for c in r["recommendations"]:
            assert c["median_wan"] <= 15.0

    def test_min_n_filter(self):
        r = recommend_cars(30, min_n=100)
        for c in r["recommendations"]:
            assert c["sample_size"] >= 100

    def test_sorted_by_score_desc(self):
        r = recommend_cars(20)
        scores = [c["score"] for c in r["recommendations"]]
        assert scores == sorted(scores, reverse=True)

    def test_top_k(self):
        r = recommend_cars(20, top_k=3)
        assert len(r["recommendations"]) == 3

    def test_budget_range(self):
        r = recommend_cars(20, budget_min_wan=10)
        for c in r["recommendations"]:
            assert 10.0 <= c["median_wan"] <= 20.0

    def test_brand_filter(self):
        r = recommend_cars(30, brand="宝马")
        for c in r["recommendations"]:
            assert c["brand"] == "宝马"

    def test_series_filter(self):
        r = recommend_cars(15, series="宝马3系")
        assert r["ok"] is True
        assert r["n_candidates"] == 1
        for c in r["recommendations"]:
            assert c["series"] == "宝马3系"

    def test_no_budget_returns_hint(self):
        r = recommend_cars(None)
        assert r["ok"] is False
        assert r["result_status"] == "empty"
        assert r["reason_code"] == "missing_budget"
        assert "预算" in r["note"]

    def test_too_tight_budget_no_crash(self):
        r = recommend_cars(0.1)
        assert r["ok"] is False
        assert r["reason_code"] in {"no_budget_matches", "below_min_sample_threshold"}

    def test_unsupported_body_type_is_explicit(self):
        r = recommend_cars(15, body_type="SUV")
        assert r["ok"] is False
        assert r["result_status"] == "error"
        assert r["reason_code"] == "unsupported_filter"

    def test_real_dataset_capabilities_are_exposed(self):
        capabilities = get_data_capabilities()
        assert capabilities["row_count"] >= 100_000
        assert {"price_yuan", "mileage_km", "production_year", "vehicle_age_years"} <= set(capabilities["fields"])
        assert {"纯电动", "插电式混合动力", "增程式"} <= set(capabilities["energy_values"])
        assert capabilities["fields"]["drive_type"]["coverage"] == 0

    def test_energy_aliases_follow_real_enum_values(self):
        values, error = normalize_energy_filter("插混")
        assert values == ["插电式混合动力"]
        assert error is None

        unsupported = recommend_cars(20, energy_type="燃油")
        assert unsupported["result_status"] == "error"
        assert unsupported["reason_code"] == "unsupported_filter"
        assert "缺失值" in unsupported["note"]

        electric = recommend_cars(30, energy_type="纯电", min_n=1)
        assert electric["energy_type"] == "纯电动"
        assert "field_coverage" in electric["data_capabilities"]

    def test_same_series_under_two_brands_is_aggregated_separately(self, monkeypatch):
        frame = pd.DataFrame({
            "price_yuan": [100000, 110000], "series": ["同名车系", "同名车系"],
            "brand_name": ["甲品牌", "乙品牌"], "energy_type": ["纯电动", "纯电动"],
            "mileage_km": [10000, 20000], "production_year": [2021, 2022],
        })
        snapshot = SimpleNamespace(version="same-name-v1", dataframe=frame)
        monkeypatch.setattr(recommend_module, "get_dataset_snapshot", lambda: snapshot)

        result = recommend_module.recommend_cars(20, min_n=1)

        assert result["dataset_version"] == "same-name-v1"
        assert {(row["brand"], row["series"], row["sample_size"])
                for row in result["recommendations"]} == {
                    ("甲品牌", "同名车系", 1), ("乙品牌", "同名车系", 1)}

    def test_valid_energy_with_no_brand_intersection_is_empty_not_unsupported(self, monkeypatch):
        frame = pd.DataFrame({
            "price_yuan": [100000, 110000], "series": ["A", "B"],
            "brand_name": ["甲品牌", "乙品牌"],
            "energy_type": ["纯电动", "插电式混合动力"],
            "mileage_km": [10000, 20000], "production_year": [2021, 2022],
        })
        monkeypatch.setattr(recommend_module, "get_dataset_snapshot",
                            lambda: SimpleNamespace(version="energy-v1", dataframe=frame))

        result = recommend_module.recommend_cars(20, brand="甲品牌", energy_type="插混", min_n=1)

        assert result["reason_code"] == "no_budget_matches"
        assert result["result_status"] == "empty"
