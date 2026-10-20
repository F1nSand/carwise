# -*- coding: utf-8 -*-
"""
同款比价工具 —— 给定车系(+可选年份/城市)，算出可比车源的价格分布

算法：
  1) 全局异常过滤：0 < price_yuan < 500万；mileage_km < 60万；年份在 [1980, 2027]
  2) 按车系(series)筛
  3) 若给年份 → production_year == year（必须相等）
  4) 若给城市 → city_name == city
  5) 组内 IQR 剔除离群价（<Q1-1.5IQR 或 >Q3+1.5IQR）
  6) 可信度分级：样本量 + 价格离散度（见 tools/common.grade_confidence）

对外导出：
  compare_same_model(series, year=None, city=None) -> dict
"""
from __future__ import annotations

import pandas as pd

from backend.data_loader import get_dataset_snapshot
from backend.tools.common import (
    apply_global_filter,
    grade_confidence,
    iqr_ratio,
    wan,
)


def compare_same_model(
    series: str,
    year: int | None = None,
    city: str | None = None,
    brand: str | None = None,
) -> dict:
    """
    同款比价主函数。

    参数：
      series  车系名（规范化后的，如 "奥迪A4L"）
      year    生产年份（可选，给了就精确匹配）
      city    车源城市（可选，给了就精确匹配）

    返回：结构化 dict（见下方 result）
    """
    snapshot = get_dataset_snapshot()
    from backend.tools.catalog_resolution import resolve_legacy_arguments
    args, error = resolve_legacy_arguments(snapshot, "price_compare", {"series": series, "brand": brand, "city": city, "year": year})
    if error:
        error.update({"series": series, "brand": brand, "year": year, "city": city})
        if error.get("reason_code") == "ambiguous_series": error["available_brands"] = sorted({x["brand_name"] for x in error.get("candidate_selectors", [])})
        return error
    series, brand, city = args["series"], args["brand"], args["city"]
    df = snapshot.dataframe

    # ---- 1) 全局异常过滤 ----
    sub, filters_applied = apply_global_filter(df)

    # ---- 2) 车系筛选 ----
    sub = sub[sub["series"] == series]
    filters_applied.append(f"车系 = {series}")
    brand_col = "brand_name" if "brand_name" in sub.columns else "品牌名称"
    available_brands = sorted(sub[brand_col].dropna().astype(str).unique()) if brand_col in sub else []
    if brand and brand_col in sub:
        sub = sub[sub[brand_col].astype(str).str.strip() == brand.strip()]
        filters_applied.append(f"品牌 = {brand}")
    elif len(available_brands) > 1:
        return {
            "ok": False, "tool": "price_compare", "result_status": "error",
            "reason_code": "ambiguous_series", "series": series, "year": year,
            "city": city, "available_brands": available_brands,
            "dataset_version": snapshot.version,
            "note": f"车系“{series}”对应多个品牌，请指定品牌后再比价。",
        }
    raw_count = int(len(sub))

    # ---- 3) 年份筛选 ----
    if year is not None:
        sub = sub[sub["production_year"] == year]
        filters_applied.append(f"生产年份 = {year}")

    # ---- 4) 城市筛选 ----
    city_col = "city_name" if "city_name" in sub.columns else "车源城市名称"
    if city is not None and city_col in sub.columns:
        sub = sub[sub[city_col].astype(str).str.strip() == city.strip()]
        filters_applied.append(f"城市 = {city}")

    count_before_iqr = int(len(sub))

    # ---- 5) 组内 IQR 剔除离群 ----
    excluded_count = 0
    if count_before_iqr > 0:
        prices = sub["price_yuan"]
        q1, q3 = prices.quantile(0.25), prices.quantile(0.75)
        iqr = q3 - q1
        if iqr > 0:
            lo, hi = q1 - 1.5 * iqr, q3 + 1.5 * iqr
            keep = (prices >= lo) & (prices <= hi)
            excluded_count = int((~keep).sum())
            sub = sub[keep]

    n = int(len(sub))

    # ---- 6) 统计 ----
    if n == 0:
        reason_code = "all_removed_by_iqr" if count_before_iqr > 0 else "no_matching_samples"
        relaxation_candidates = []
        if city:
            relaxation_candidates.append({"field": "city", "action": "remove", "auto": True, "label": "去掉城市限制"})
        if year is not None:
            relaxation_candidates.append({"field": "year", "action": "remove", "auto": True, "label": "放宽生产年份"})
        if count_before_iqr > 0:
            relaxation_candidates.append({"field": "iqr", "action": "review", "auto": False, "label": "检查异常值过滤"})
        return {
            "ok": False,
            "tool": "price_compare",
            "result_status": "empty",
            "reason_code": reason_code,
            "series": series,
            "brand": brand,
            "year": year,
            "city": city,
            "filters_applied": filters_applied,
            "raw_count": raw_count,
            "excluded_count": 0,
            "sample_size": 0,
            "price": None,
            "confidence": "none",
            "representative_samples": [],
            "observed": {"raw_count": raw_count, "count_before_iqr": count_before_iqr, "excluded_count": excluded_count},
            "relaxation_candidates": relaxation_candidates,
            "note": "未找到符合条件的可比车源，无法给出价格结论。",
            "dataset_version": snapshot.version,
        }

    prices = sub["price_yuan"]
    p25 = float(prices.quantile(0.25))
    median = float(prices.median())
    p75 = float(prices.quantile(0.75))
    mean = float(prices.mean())
    pmin, pmax = float(prices.min()), float(prices.max())

    ratio = iqr_ratio(prices)
    confidence = grade_confidence(n, ratio)

    # 可信度说明（区分"样本少"与"离散大"两种成因）
    note = ""
    if confidence == "low":
        if n < 10:
            note = f"样本量不足（仅 {n} 台），结论仅供参考，不建议据此做重大决策。"
        else:
            note = "样本量达标但价格离散度较大（IQR/中位≥1.0），价格区间宽，结论仅供参考。"
    elif confidence == "medium":
        if n < 30:
            note = "样本量一般（10~29 台），结论有一定参考价值。"
        else:
            note = "样本量充足，但价格波动较明显，结论供参考。"

    # ---- 代表性车源：价格最接近中位数的 3 条 ----
    sub_sorted = sub.assign(_d=(sub["price_yuan"] - median).abs()).sort_values("_d")
    reps = []
    for _, r in sub_sorted.head(3).iterrows():
        reps.append({
            "car_name": str(r.get("car_name", "")),
            "year": int(r["production_year"]) if pd.notna(r.get("production_year")) else None,
            "city": str(r.get(city_col, "")),
            "price_yuan": float(r["price_yuan"]),
            "mileage_km": float(r["mileage_km"]) if pd.notna(r.get("mileage_km")) else None,
            "url": str(r.get("详情链接", "")) if "详情链接" in sub.columns else "",
        })

    return {
        "ok": True,
        "tool": "price_compare",
        "result_status": "success",
        "series": series,
        "brand": brand,
        "year": year,
        "city": city,
        "filters_applied": filters_applied,
        "raw_count": raw_count,
        "count_before_iqr": count_before_iqr,
        "excluded_count": excluded_count,
        "sample_size": n,
        "price": {
            "p25_yuan": p25, "median_yuan": median, "p75_yuan": p75,
            "mean_yuan": mean, "min_yuan": pmin, "max_yuan": pmax,
            "p25_wan": wan(p25), "median_wan": wan(median), "p75_wan": wan(p75),
            "mean_wan": wan(mean),
        },
        "dispersion": {"iqr_ratio": round(ratio, 3) if ratio is not None else None},
        "confidence": confidence,
        "representative_samples": reps,
        "note": note,
        "dataset_version": snapshot.version,
    }
