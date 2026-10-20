# -*- coding: utf-8 -*-
"""
价格衰减工具的单元测试

运行：
  python -m pytest tests/test_price_decay.py -q
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.tools.price_decay import price_decay_curve   # noqa: E402
import backend.tools.price_decay as decay_module


class TestPriceDecay:
    def test_shape(self):
        r = price_decay_curve("宝马3系")
        assert r["ok"] is True
        assert r["tool"] == "price_decay"
        assert r["n_years"] >= 2
        assert len(r["points"]) == r["n_years"]
        for key in ("year_range", "annual_depreciation_pct", "total_change_pct",
                    "points", "confidence"):
            assert key in r

    def test_points_sorted_ascending(self):
        r = price_decay_curve("宝马3系")
        years = [p["year"] for p in r["points"]]
        assert years == sorted(years)

    def test_min_year_n_filter(self):
        r = price_decay_curve("宝马3系", min_year_n=50)
        for p in r["points"]:
            assert p["sample_size"] >= 50

    def test_yoy_first_point_is_none(self):
        r = price_decay_curve("宝马3系")
        assert r["points"][0]["yoy_pct"] is None
        # 后续点应有同比
        assert r["points"][1]["yoy_pct"] is not None

    def test_newer_year_more_expensive(self):
        """真实数据：宝马3系越新的年款中位价越高（年均折旧率为正=越老越便宜）。"""
        r = price_decay_curve("宝马3系")
        assert r["points"][-1]["median_yuan"] > r["points"][0]["median_yuan"]
        assert r["annual_depreciation_pct"] > 0

    def test_city_filter(self):
        r = price_decay_curve("宝马3系", city="北京")
        assert r["ok"] is True
        assert r["city"] == "北京"

    def test_unknown_series_returns_catalog_error(self):
        r = price_decay_curve("不存在的车系XYZ")
        assert r["ok"] is False
        assert r["result_status"] == "error"
        assert r["reason_code"] == "unknown_series"
        assert r["keyword_field"] == "series"

    def test_unreasonable_threshold_no_crash(self):
        r = price_decay_curve("宝马3系", min_year_n=99999)
        assert r["ok"] is False

    def test_missing_years_are_interval_changes_not_yoy(self, monkeypatch):
        frame = pd.DataFrame({
            "price_yuan": [100000, 120000], "series": ["间隔车系", "间隔车系"],
            "brand_name": ["甲", "甲"], "production_year": [2020, 2024],
            "mileage_km": [10000, 10000],
        })
        monkeypatch.setattr(decay_module, "get_dataset_snapshot",
                            lambda: SimpleNamespace(version="gap-v1", dataframe=frame))

        result = decay_module.price_decay_curve("间隔车系", min_year_n=1)

        newest = result["points"][1]
        assert newest["year_delta"] == 4
        assert newest["yoy_pct"] is None
        assert newest["interval_change_pct"] == 20.0
        assert result["annual_depreciation_pct"] == 4.5
        assert "横截面挂牌价差" in result["note"]

    def test_cross_brand_series_is_rejected_as_ambiguous(self, monkeypatch):
        frame = pd.DataFrame({
            "price_yuan": [100000, 120000], "series": ["同名", "同名"],
            "brand_name": ["甲", "乙"], "production_year": [2020, 2021],
            "mileage_km": [10000, 10000],
        })
        monkeypatch.setattr(decay_module, "get_dataset_snapshot",
                            lambda: SimpleNamespace(version="ambiguous-v1", dataframe=frame))

        result = decay_module.price_decay_curve("同名", min_year_n=1)

        assert result["reason_code"] == "ambiguous_series"
        assert result["available_brands"] == ["乙", "甲"] or result["available_brands"] == ["甲", "乙"]
