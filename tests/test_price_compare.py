# -*- coding: utf-8 -*-
"""
同款比价工具与意图识别的单元测试

运行：
  python -m pytest tests/test_price_compare.py -q
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.tools.price_compare import compare_same_model  # noqa: E402
import backend.tools.price_compare as compare_module


class TestCompareSameModel:
    def test_basic_result_shape(self):
        r = compare_same_model("奥迪A4L", year=2022)
        assert r["ok"] is True
        assert r["series"] == "奥迪A4L"
        assert r["year"] == 2022
        assert r["sample_size"] > 0
        for k in ("p25_yuan", "median_yuan", "p75_yuan"):
            assert k in r["price"]

    def test_quantile_ordering(self):
        """P25 <= 中位 <= P75。"""
        r = compare_same_model("奥迪A4L", year=2022)
        p = r["price"]
        assert p["p25_yuan"] <= p["median_yuan"] <= p["p75_yuan"]

    def test_wan_conversion(self):
        """元转万：558000 元应约 55.8 万。"""
        r = compare_same_model("奥迪A4L", year=2022)
        assert r["price"]["median_wan"] == pytest.approx(
            r["price"]["median_yuan"] / 10000.0, abs=0.01)

    def test_confidence_levels(self):
        """样本充足 -> high。"""
        r = compare_same_model("奥迪A4L", year=2022)
        assert r["confidence"] == "high"

    def test_year_filter_reduces_sample(self):
        """加年份后样本应 <= 不加年份。"""
        all_years = compare_same_model("奥迪A4L")
        one_year = compare_same_model("奥迪A4L", year=2022)
        assert one_year["sample_size"] <= all_years["sample_size"]

    def test_unknown_series_returns_catalog_error(self):
        r = compare_same_model("不存在的车系XYZ")
        assert r["ok"] is False
        assert r["result_status"] == "error"
        assert r["reason_code"] == "unknown_series"
        assert r["keyword_field"] == "series"
        assert r["requested_series"] == "不存在的车系XYZ"

    def test_city_filter(self):
        r = compare_same_model("宝马X5", city="上海")
        assert r["ok"] is True
        assert r["city"] == "上海"
        for s in r["representative_samples"]:
            assert s["city"] == "上海"

    def test_representative_samples_count(self):
        r = compare_same_model("奥迪A4L", year=2022)
        assert 0 < len(r["representative_samples"]) <= 3

    def test_same_name_across_brands_requires_disambiguation(self, monkeypatch):
        frame = pd.DataFrame({"price_yuan": [100000, 120000], "series": ["同名", "同名"],
                              "brand_name": ["甲", "乙"], "production_year": [2020, 2021],
                              "mileage_km": [10000, 10000], "city_name": ["北京", "北京"]})
        monkeypatch.setattr(compare_module, "get_dataset_snapshot",
                            lambda: SimpleNamespace(version="ambiguous-v1", dataframe=frame))

        result = compare_module.compare_same_model("同名")

        assert result["reason_code"] == "ambiguous_series"
        assert set(result["available_brands"]) == {"甲", "乙"}
        assert result["dataset_version"] == "ambiguous-v1"

    def test_explicit_brand_filters_same_name(self, monkeypatch):
        frame = pd.DataFrame({"price_yuan": [100000, 120000], "series": ["同名", "同名"],
                              "brand_name": ["甲", "乙"], "production_year": [2020, 2021],
                              "mileage_km": [10000, 10000], "city_name": ["北京", "北京"]})
        monkeypatch.setattr(compare_module, "get_dataset_snapshot",
                            lambda: SimpleNamespace(version="ambiguous-v1", dataframe=frame))

        result = compare_module.compare_same_model("同名", brand="甲")

        assert result["ok"] is True
        assert result["sample_size"] == 1
