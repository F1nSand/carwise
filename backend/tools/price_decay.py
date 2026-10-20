# -*- coding: utf-8 -*-
"""
价格衰减工具 —— 同一车系「随生产年份」的挂牌价折旧曲线

用途：回答“某款车每年掉价多少”“保值吗”“哪年款最划算”这类问题。

算法：
  1) 全局异常过滤（价格/里程/年份）
  2) 按车系(series)筛选，可选限定城市
  3) 按 生产年份 分组，算各年份中位挂牌价 + 样本量
  4) 只保留样本量 >= min_year_n 的年份（保证每年中位价可信）
  5) 按相邻年份计算 同比变化（元 / 万元 / 百分比）
  6) 汇总：样本区间内 年均折旧率（每老一个年款平均便宜多少）、总变化、最新/最老年份中位价

说明：
  - 数据里的「原产地价格」≈ 当期挂牌价，**不是**新车指导价，
    因此本工具刻画的是「不同年款之间的相对价差」，不是「相对新车价的折旧率」。
  - 相邻年份降幅会受车型改款影响（改款年可能不降反升），属正常现象。

对外导出：
  price_decay_curve(series, city=None, min_year_n=10) -> dict
"""
from __future__ import annotations

from backend.data_loader import get_dataset_snapshot
from backend.tools.common import apply_global_filter, grade_confidence, wan

DEFAULT_MIN_YEAR_N = 10


def price_decay_curve(
    series: str,
    city: str | None = None,
    min_year_n: int = DEFAULT_MIN_YEAR_N,
    brand: str | None = None,
) -> dict:
    """
    价格衰减主函数。

    参数：
      series      车系名（规范化后，如 "宝马3系"）
      city        车源城市（可选，给了就只看该城市）
      min_year_n  每个年份至少多少台才纳入（默认 10）
    """
    snapshot = get_dataset_snapshot()
    from backend.tools.catalog_resolution import resolve_legacy_arguments
    args, error = resolve_legacy_arguments(snapshot, "price_decay", {"series": series, "brand": brand, "city": city})
    if error:
        error.update({"series": series, "brand": brand, "city": city})
        if error.get("reason_code") == "ambiguous_series": error["available_brands"] = sorted({x["brand_name"] for x in error.get("candidate_selectors", [])})
        return error
    series, brand, city = args["series"], args["brand"], args["city"]
    df = snapshot.dataframe
    sub, filters_applied = apply_global_filter(df)

    # 车系
    sub = sub[sub["series"] == series]
    filters_applied.append(f"车系 = {series}")
    brand_col = "brand_name" if "brand_name" in sub.columns else "品牌名称"
    available_brands = sorted(sub[brand_col].dropna().astype(str).unique()) if brand_col in sub else []
    if brand and brand_col in sub:
        sub = sub[sub[brand_col].astype(str).str.strip() == brand.strip()]
        filters_applied.append(f"品牌 = {brand}")
    elif len(available_brands) > 1:
        return {
            "ok": False, "tool": "price_decay", "result_status": "error",
            "reason_code": "ambiguous_series", "series": series,
            "city": city, "available_brands": available_brands,
            "dataset_version": snapshot.version,
            "note": f"车系“{series}”对应多个品牌，请指定品牌后再分析价格变化。",
        }

    # 可选城市
    city_col = "city_name" if "city_name" in sub.columns else "车源城市名称"
    if city is not None and city_col in sub.columns:
        sub = sub[sub[city_col].astype(str).str.strip() == city.strip()]
        filters_applied.append(f"城市 = {city}")

    # 去掉无年份
    sub = sub[sub["production_year"].notna()]
    raw_count = int(len(sub))

    if raw_count == 0:
        return _empty(series, city, min_year_n, filters_applied,
                      "未找到该车源，无法做价格衰减分析。", reason_code="no_matching_samples",
                      brand=brand, dataset_version=snapshot.version)

    # 按年份聚合
    grouped = (
        sub.groupby(sub["production_year"].astype(int))["price_yuan"]
        .agg(["median", "count"])
        .reset_index()
    )
    grouped.columns = ["year", "median_yuan", "sample_size"]
    grouped = grouped.sort_values("year")

    # 年份门槛
    kept = grouped[grouped["sample_size"] >= min_year_n].copy()
    filters_applied.append(f"仅纳入样本量 >= {min_year_n} 台的年份")

    if len(kept) < 2:
        return _empty(
            series, city, min_year_n, filters_applied,
            f"样本充足的年份不足（需至少 2 个年份、每年 >= {min_year_n} 台），"
            "无法画出衰减曲线（可放宽门槛或换更热门的车系）。",
            raw_count=raw_count,
            reason_code="below_min_year_threshold",
            relaxation_candidates=[{"field": "min_year_n", "action": "lower", "auto": True, "label": "降低年份样本门槛"}],
            brand=brand, dataset_version=snapshot.version,
        )

    # 逐点 + 同比
    points = []
    prev_med = None
    for _, r in kept.iterrows():
        med = float(r["median_yuan"])
        pt = {
            "year": int(r["year"]),
            "sample_size": int(r["sample_size"]),
            "median_yuan": med,
            "median_wan": wan(med),
            "year_delta": None if not points else int(r["year"]) - points[-1]["year"],
            "interval_delta_wan": None,
            "interval_change_pct": None,
            "yoy_delta_wan": None,
            "yoy_pct": None,
        }
        if prev_med is not None and prev_med > 0:
            year_delta = pt["year_delta"]
            pt["interval_delta_wan"] = wan(med - prev_med)
            pt["interval_change_pct"] = round((med - prev_med) / prev_med * 100.0, 1)
            if year_delta == 1:
                pt["yoy_delta_wan"] = pt["interval_delta_wan"]
                pt["yoy_pct"] = pt["interval_change_pct"]
        points.append(pt)
        prev_med = med

    # Annualize each observed cross-sectional price interval by its actual year span.
    dep_list = []
    for older, newer in zip(points, points[1:]):
        year_delta = newer["year"] - older["year"]
        if newer["median_yuan"] > 0 and year_delta > 0:
            dep_list.append(
                (1 - (older["median_yuan"] / newer["median_yuan"]) ** (1 / year_delta)) * 100.0
            )
    annual_depreciation_pct = round(sum(dep_list) / len(dep_list), 1) if dep_list else None

    oldest, newest = points[0], points[-1]
    span_years = newest["year"] - oldest["year"]
    total_pct = None
    if oldest["median_yuan"] > 0 and span_years > 0:
        total_pct = round(
            (newest["median_yuan"] - oldest["median_yuan"]) / oldest["median_yuan"] * 100.0, 1
        )

    # 可信度：用全量样本量 + 整体离散度
    from backend.tools.common import iqr_ratio
    ratio = iqr_ratio(sub["price_yuan"])
    confidence = grade_confidence(raw_count, ratio)

    note = (f"基于 {raw_count} 条车源、{len(points)} 个样本充足的年份（"
            f"{oldest['year']}~{newest['year']} 年款）。价格为挂牌价。"
            "这是不同生产年份车源的横截面挂牌价差，不代表同一车辆的实际折旧；"
            "跨年缺口按实际年数年化，同比仅对连续年款显示。")
    if confidence == "low":
        note += " 价格离散度较大，衰减曲线仅供参考。"

    return {
        "ok": True,
        "tool": "price_decay",
        "result_status": "success",
        "series": series,
        "brand": brand,
        "city": city,
        "min_year_n": min_year_n,
        "filters_applied": filters_applied,
        "raw_count": raw_count,
        "n_years": len(points),
        "year_range": [oldest["year"], newest["year"]],
        "annual_depreciation_pct": annual_depreciation_pct,   # 年均折旧率（正=越老越便宜）
        "total_change_pct": total_pct,                        # 最老→最新 总变化
        "points": points,
        "confidence": confidence,
        "note": note,
        "dataset_version": snapshot.version,
    }


def _empty(series, city, min_year_n, filters_applied, msg, raw_count=0,
           reason_code="no_data", result_status="empty", relaxation_candidates=None,
           brand=None, dataset_version=None) -> dict:
    return {
        "ok": False,
        "tool": "price_decay",
        "result_status": result_status,
        "reason_code": reason_code,
        "series": series,
        "brand": brand,
        "city": city,
        "min_year_n": min_year_n,
        "filters_applied": filters_applied,
        "raw_count": raw_count,
        "n_years": 0,
        "year_range": None,
        "annual_depreciation_pct": None,
        "total_change_pct": None,
        "points": [],
        "confidence": "none",
        "observed": {"raw_count": raw_count, "min_year_n": min_year_n},
        "relaxation_candidates": relaxation_candidates or [],
        "note": msg,
        "dataset_version": dataset_version,
    }
