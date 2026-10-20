# -*- coding: utf-8 -*-
"""
区域价差工具 + 可信度算法 的单元测试

运行：
  python -m pytest tests/test_region_gap.py -q
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.tools.common import grade_confidence               # noqa: E402
from backend.tools.region_gap import region_price_gap           # noqa: E402


class TestGradeConfidence:
    """可信度分级：样本量 + 离散度。"""

    def test_high_sample_low_dispersion(self):
        # 奥迪A4L 场景：样本足、离散度 0.47
        assert grade_confidence(1905, 0.47) == "high"

    def test_high_sample_high_dispersion_downgraded(self):
        # 宝马X5@上海 场景：样本 35 但离散度 1.26 → 降两级到 low
        assert grade_confidence(35, 1.26) == "low"

    def test_medium_sample_high_dispersion(self):
        assert grade_confidence(20, 1.2) == "low"

    def test_small_sample(self):
        assert grade_confidence(5, 0.2) == "low"

    def test_high_sample_mild_dispersion_downgrade_one(self):
        # 0.5~1.0 降一级
        assert grade_confidence(35, 0.7) == "medium"

    def test_no_dispersion_falls_back_to_sample(self):
        assert grade_confidence(35) == "high"
        assert grade_confidence(15) == "medium"
        assert grade_confidence(3) == "low"


class TestRegionPriceGap:
    def test_shape(self):
        r = region_price_gap("宝马3系")
        assert r["ok"] is True
        assert r["tool"] == "region_gap"
        assert len(r["cities"]) > 0
        for key in ("national_median_yuan", "national_median_wan",
                    "n_cities_included", "cheapest_cities", "priciest_cities",
                    "confidence"):
            assert key in r

    def test_min_city_n_filter(self):
        r = region_price_gap("宝马3系", min_city_n=15)
        for c in r["cities"]:
            assert c["sample_size"] >= 15

    def test_sorting_ascending(self):
        r = region_price_gap("宝马3系")
        medians = [c["median_yuan"] for c in r["cities"]]
        assert medians == sorted(medians)

    def test_direction_real(self):
        """真实集成：宝马3系应含低于全国的城市(沈阳)与高于全国的城市(成都)。"""
        r = region_price_gap("宝马3系")
        by_city = {c["city"]: c for c in r["cities"]}
        if "沈阳" in by_city:
            assert by_city["沈阳"]["direction"] == "below"
        if "成都" in by_city:
            assert by_city["成都"]["direction"] == "above"

    def test_unreasonable_threshold_no_crash(self):
        """门槛过高 → 不崩溃，返回 ok=False 或空城市。"""
        r = region_price_gap("宝马3系", min_city_n=99999)
        assert r["ok"] is False
        assert r["cities"] == []

    def test_unknown_series(self):
        r = region_price_gap("不存在的车系XYZ")
        assert r["ok"] is False
