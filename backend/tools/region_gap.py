# -*- coding: utf-8 -*-
"""
区域价差工具 —— 同一车系在不同城市的价格差异

算法：
  1) 全局异常过滤（价格/里程/年份）
  2) 按车系（可选年份）筛选
  3) 按城市聚合：各城市价格中位数 + 样本量
  4) 只保留样本量 >= min_city_n 的城市（保证中位数可信）
  5) 计算全国中位数，逐城算与全国的差值/百分比/方向
  6) 按城市中位价排序，取最便宜/最贵 Top3

对外导出：
  region_price_gap(series, year=None, min_city_n=15) -> dict
"""
from __future__ import annotations

from backend.config import TH
from backend.data_loader import get_dataset_snapshot
from backend.tools.common import apply_global_filter, grade_confidence, iqr_ratio, wan
from backend.analysis.region import compute_city_stats

TOP_K = 3


def region_price_gap(
    series: str,
    year: int | None = None,
    min_city_n: int = 15,
) -> dict:
    """
    区域价差主函数。

    参数：
      series      车系名（规范化后，如 "宝马3系"）
      year        生产年份（可选）
      min_city_n  城市纳入门槛（该城市至少多少台）
    """
    snapshot = get_dataset_snapshot()
    from backend.tools.catalog_resolution import resolve_legacy_arguments
    args, catalog_error = resolve_legacy_arguments(snapshot, "region_gap", {"series": series, "year": year})
    if catalog_error:
        catalog_error.update({"series": series, "requested_series": series, "year": year, "min_city_n": min_city_n})
        return catalog_error
    series, brand = args["series"], args["brand"]
    df = snapshot.dataframe
    sub, filters_applied = apply_global_filter(df)

    # 按车系
    sub = sub[(sub["series"] == series) & (sub["brand_name"].astype(str).str.strip() == brand)]
    filters_applied.extend([f"车系 = {series}", f"品牌 = {brand}"])

    # 可选年份
    if year is not None:
        sub = sub[sub["production_year"] == year]
        filters_applied.append(f"生产年份 = {year}")

    city_col = "city_name" if "city_name" in sub.columns else "车源城市名称"
    if city_col not in sub.columns:
        return _empty(series, year, min_city_n, filters_applied, "数据中无城市字段。", reason_code="unsupported_filter", result_status="error")

    # 去掉空城市
    sub = sub[sub[city_col].notna() & (sub[city_col].astype(str).str.strip() != "")]
    raw_count = int(len(sub))
    if raw_count == 0:
        return _empty(series, year, min_city_n, filters_applied,
                      "未找到该车源，无法做区域价差分析。", reason_code="no_matching_samples")

    # Use the same grouped statistics core as the dashboard while preserving the legacy result shape.
    valid = sub[["price_yuan", city_col]].copy()
    valid["_city"] = valid[city_col].astype(str).str.strip()
    national_median, shared_rows = compute_city_stats(valid, min_city_n)
    raw_count = int(len(valid))
    ratio = iqr_ratio(valid["price_yuan"])
    filters_applied.append(f"仅纳入样本量 >= {min_city_n} 台的城市")
    if not any(row["eligible"] for row in shared_rows):
        return _empty(
            series, year, min_city_n, filters_applied,
            f"没有城市的样本量达到 {min_city_n} 台，无法比较区域价差（可放宽门槛）。",
            raw_count=raw_count, national_median=national_median,
            reason_code="below_min_city_threshold",
            relaxation_candidates=[{"field": "min_city_n", "action": "lower", "auto": True, "label": "降低城市样本门槛"}],
        )
    cells = []
    for row in shared_rows:
        if not row["eligible"]:
            continue
        med = float(row["median_yuan"])
        diff = float(row["diff_yuan"])
        pct = float(row["diff_pct"]) if row["diff_pct"] is not None else None
        cells.append({
            "city": row["city"], "sample_size": int(row["n"]),
            "median_yuan": med, "median_wan": wan(med),
            "diff_vs_national_yuan": diff, "diff_vs_national_wan": wan(diff),
            "diff_pct": round(pct, 1) if pct is not None else None,
            "direction": "above" if diff > 0 else ("below" if diff < 0 else "same"),
        })
    cells.sort(key=lambda cell: cell["median_yuan"])

    def _brief(c: dict) -> dict:
        return {"city": c["city"], "median_wan": c["median_wan"],
                "diff_pct": c["diff_pct"], "direction": c["direction"]}

    confidence = grade_confidence(raw_count, ratio)
    note = (f"已按各城市样本量 >= {min_city_n} 台筛选，纳入 {len(cells)} 个城市；"
            f"全国中位基于 {raw_count} 条车源。价格均为挂牌价。")
    if confidence == "low":
        note += " 整体价格离散度较大或样本有限，区域结论仅供参考。"

    return {
        "ok": True,
        "tool": "region_gap",
        "result_status": "success",
        "series": series,
        "year": year,
        "min_city_n": min_city_n,
        "filters_applied": filters_applied,
        "raw_count": raw_count,
        "n_cities_included": len(cells),
        "national_median_yuan": national_median,
        "national_median_wan": wan(national_median),
        "cities": cells,
        "cheapest_cities": [_brief(c) for c in cells[:TOP_K]],
        "priciest_cities": [_brief(c) for c in cells[-TOP_K:][::-1]],
        "confidence": confidence,
        "note": note,
    }


def _empty(series, year, min_city_n, filters_applied, msg,
           raw_count=0, national_median=None, reason_code="no_data",
           result_status="empty", relaxation_candidates=None) -> dict:
    return {
        "ok": False,
        "tool": "region_gap",
        "result_status": result_status,
        "reason_code": reason_code,
        "series": series,
        "year": year,
        "min_city_n": min_city_n,
        "filters_applied": filters_applied,
        "raw_count": raw_count,
        "n_cities_included": 0,
        "national_median_yuan": national_median,
        "national_median_wan": wan(national_median) if national_median else None,
        "cities": [],
        "cheapest_cities": [],
        "priciest_cities": [],
        "confidence": "none",
        "observed": {"raw_count": raw_count, "min_city_n": min_city_n},
        "relaxation_candidates": relaxation_candidates or [],
        "note": msg,
    }
