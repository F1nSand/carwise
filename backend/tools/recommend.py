# -*- coding: utf-8 -*-
"""
购车推荐工具 —— 给定预算（可带城市/品牌/能源类型），推荐合适的车系

用途：回答“15 万能买什么车”“预算 20 万买什么 SUV”“想买新能源，10 万以内有啥”这类问题。

算法：
  1) 全局异常过滤（价格/里程/年份）
  2) 按预算区间筛（budget_min_wan ~ budget_max_wan 万）
  3) 可选：限定城市 / 品牌 / 能源类型 / 变速箱
  4) 按车系聚合：中位价、主流价区间、样本量、中位车龄、中位里程
  5) 只保留样本量 >= min_n 的车系（保证推荐有代表性）
  6) 综合打分排序（权重写死在常量里，可解释、非黑盒）：
        score = 0.5 * 热度(样本量) + 0.3 * 车龄新 + 0.2 * 里程低
     三项都做 min-max 归一化到 0~1 后加权。
  7) 取 Top K 作为推荐清单

对外导出：
  recommend_cars(budget_max_wan, budget_min_wan=0, city=None, brand=None,
                 energy_type=None, top_k=10, min_n=15) -> dict
"""
from __future__ import annotations

import pandas as pd

from backend.data_loader import get_dataset_snapshot
from backend.tools.common import apply_global_filter, grade_confidence, wan
from backend.tools.data_contract import contract_summary, get_data_capabilities, normalize_energy_filter

# 推荐综合打分的权重（可解释）：热度 / 车龄新 / 里程低
W_POPULARITY = 0.5
W_NEWNESS = 0.3
W_LOW_MILEAGE = 0.2

DEFAULT_TOP_K = 10
DEFAULT_MIN_N = 15


def _minmax(series: pd.Series) -> pd.Series:
    """把一列线性归一化到 0~1；全相等时返回 0.5（中性）。"""
    lo, hi = series.min(), series.max()
    if pd.isna(lo) or pd.isna(hi) or hi == lo:
        return pd.Series(0.5, index=series.index)
    return (series - lo) / (hi - lo)


def recommend_cars(
    budget_max_wan: float | None,
    budget_min_wan: float = 0.0,
    city: str | None = None,
    brand: str | None = None,
    series: str | None = None,
    energy_type: str | None = None,
    body_type: str | None = None,
    max_age: int | None = None,
    top_k: int = DEFAULT_TOP_K,
    min_n: int = DEFAULT_MIN_N,
) -> dict:
    """
    购车推荐主函数。

    参数：
      budget_max_wan  预算上限（万元，必填）
      budget_min_wan  预算下限（万元，默认 0）
      city            限定城市（可选）
      brand           限定品牌（可选），如 "宝马"
      series          限定车系（可选），如 "宝马3系"
      energy_type     限定能源类型（可选）：纯电动、插电式混合动力、增程式；支持“纯电/插混/增程”别名
      top_k           返回的推荐数量
      min_n           车系纳入门槛（至少多少台）
    """
    snapshot = get_dataset_snapshot()
    from backend.tools.catalog_resolution import resolve_legacy_arguments
    args, catalog_error = resolve_legacy_arguments(snapshot, "recommend", {
        "budget_min_wan": budget_min_wan, "budget_max_wan": budget_max_wan,
        "city": city, "brand": brand, "series": series, "energy_type": energy_type,
        "max_age": max_age,
    })
    if catalog_error: return catalog_error
    city, brand, series = args.get("city"), args.get("brand"), args.get("series")
    if body_type:
        capabilities = get_data_capabilities(snapshot)
        return _empty(
            budget_max_wan, budget_min_wan, city, brand, series, energy_type,
            "当前清洗数据没有可靠的车型类别字段，不能把 SUV/轿车条件当作已筛选。",
            reason_code="unsupported_filter",
            result_status="error",
            observed={"unsupported_field": "body_type", "data_capabilities": capabilities},
            snapshot=snapshot,
        )

    # 预算校验
    if budget_max_wan is None or float(budget_max_wan) <= 0:
        return _empty(
            budget_max_wan, budget_min_wan, city, brand, series, energy_type,
            "还没有预算信息，告诉我预算上限（如「15 万以内」）我才能推荐。",
            reason_code="missing_budget",
            snapshot=snapshot,
        )
    budget_max_wan = float(budget_max_wan)
    budget_min_wan = float(budget_min_wan or 0.0)
    if budget_min_wan > budget_max_wan:
        budget_min_wan, budget_max_wan = budget_max_wan, budget_min_wan

    df = snapshot.dataframe
    energy_values, energy_error = normalize_energy_filter(energy_type, snapshot)
    if energy_error:
        return _empty(
            budget_max_wan, budget_min_wan, city, brand, series, energy_type,
            energy_error,
            reason_code="unsupported_filter",
            result_status="error",
            observed={"unsupported_field": "energy_type", "requested": energy_type, "data_capabilities": contract_summary(snapshot)},
            snapshot=snapshot,
        )
    sub, filters_applied = apply_global_filter(df)

    # 预算筛
    lo_yuan, hi_yuan = budget_min_wan * 10000.0, budget_max_wan * 10000.0
    sub = sub[(sub["price_yuan"] >= lo_yuan) & (sub["price_yuan"] <= hi_yuan)]
    filters_applied.append(f"挂牌价在 {budget_min_wan:g}~{budget_max_wan:g} 万元之间")

    # 可选城市
    city_col = "city_name" if "city_name" in sub.columns else "车源城市名称"
    if city and city_col in sub.columns:
        sub = sub[sub[city_col].astype(str).str.strip() == city.strip()]
        filters_applied.append(f"城市 = {city}")

    # 可选品牌
    brand_col = "brand_name" if "brand_name" in sub.columns else "品牌名称"
    if brand and brand_col in sub.columns:
        sub = sub[sub[brand_col].astype(str).str.strip() == brand.strip()]
        filters_applied.append(f"品牌 = {brand}")

    # 可选车系
    if series:
        sub = sub[sub["series"] == series]
        filters_applied.append(f"车系 = {series}")

    # 可选能源类型
    if energy_values and "energy_type" in sub.columns:
        sub = sub[sub["energy_type"].isin(energy_values)]
        filters_applied.append(f"能源类型 = {'、'.join(energy_values)}")

    if max_age is not None:
        if "vehicle_age_years" not in sub.columns:
            return _empty(
                budget_max_wan, budget_min_wan, city, brand, series, energy_type,
                "数据中没有可用车龄字段，无法应用车龄条件。",
                reason_code="unsupported_filter",
                result_status="error",
                filters_applied=filters_applied,
                observed={"unsupported_field": "max_age"},
                snapshot=snapshot,
            )
        sub = sub[sub["vehicle_age_years"] <= int(max_age)]
        filters_applied.append(f"车龄 <= {int(max_age)} 年")

    budget_count = int(len(sub))
    if budget_count == 0:
        return _empty(
            budget_max_wan, budget_min_wan, city, brand, series, energy_type,
            "该预算/条件下没有找到车源，试着放宽预算或去掉城市/品牌限制。",
            budget_count=budget_count, filters_applied=filters_applied,
            reason_code="no_budget_matches",
            observed={"budget_count": budget_count},
            relaxation_candidates=[
                *([{"field": "city", "action": "remove", "auto": True, "label": "去掉城市限制"}] if city else []),
                *([{"field": "brand", "action": "remove", "auto": True, "label": "去掉品牌限制"}] if brand else []),
                *([{"field": "max_age", "action": "widen", "auto": True, "label": "放宽车龄限制"}] if max_age is not None else []),
            ],
            snapshot=snapshot,
        )

    # 按车系聚合（基础统计 + 分位数分开算，写法更稳）
    group_columns = [brand_col, "series"] if brand_col in sub.columns else ["series"]
    g = sub.groupby(group_columns, dropna=False, sort=True)
    base = g.agg(
        median_yuan=("price_yuan", "median"),
        sample_size=("price_yuan", "count"),
    )
    if "production_year" in sub.columns:
        base["median_year"] = g["production_year"].median()
    if "mileage_km" in sub.columns:
        base["mileage_km"] = g["mileage_km"].median()

    q = sub.groupby(group_columns, dropna=False, sort=True)["price_yuan"].quantile([0.25, 0.75]).unstack()
    q.columns = ["p25_yuan", "p75_yuan"]
    grouped = base.join(q).reset_index()
    if brand_col in sub.columns:
        grouped = grouped.rename(columns={brand_col: "brand"})

    grouped = grouped[grouped["sample_size"] >= min_n].copy()
    filters_applied.append(f"仅纳入样本量 >= {min_n} 台的车系")

    if len(grouped) == 0:
        return _empty(
            budget_max_wan, budget_min_wan, city, brand, series, energy_type,
            f"该预算下没有样本量 >= {min_n} 台的车系，试着放宽预算或降低门槛。",
            budget_count=budget_count, filters_applied=filters_applied,
            reason_code="below_min_sample_threshold",
            observed={"budget_count": budget_count, "min_n": min_n, "candidate_series_before_threshold": int(base.shape[0])},
            relaxation_candidates=[{"field": "min_n", "action": "lower", "auto": True, "label": "降低车系样本门槛"}],
            snapshot=snapshot,
        )

    # 综合打分
    grouped["_pop"] = _minmax(grouped["sample_size"].astype(float))
    if "median_year" in grouped.columns:
        grouped["_new"] = _minmax(grouped["median_year"].astype(float))
    else:
        grouped["_new"] = 0.5
    if "mileage_km" in grouped.columns:
        grouped["_mil"] = 1.0 - _minmax(grouped["mileage_km"].astype(float))
    else:
        grouped["_mil"] = 0.5

    grouped["score"] = (
        W_POPULARITY * grouped["_pop"]
        + W_NEWNESS * grouped["_new"]
        + W_LOW_MILEAGE * grouped["_mil"]
    ).round(3)

    grouped = grouped.sort_values(["score", "sample_size"], ascending=False)
    top = grouped.head(top_k)

    recs = []
    for _, r in top.iterrows():
        recs.append({
            "series": str(r["series"]),
            "brand": str(r["brand"]) if "brand" in r and pd.notna(r.get("brand")) else None,
            "sample_size": int(r["sample_size"]),
            "median_wan": wan(float(r["median_yuan"])),
            "p25_wan": wan(float(r["p25_yuan"])) if pd.notna(r.get("p25_yuan")) else None,
            "p75_wan": wan(float(r["p75_yuan"])) if pd.notna(r.get("p75_yuan")) else None,
            "median_year": int(r["median_year"]) if "median_year" in r and pd.notna(r.get("median_year")) else None,
            "median_mileage_wan_km": round(float(r["mileage_km"]) / 10000.0, 1)
            if "mileage_km" in r and pd.notna(r.get("mileage_km")) else None,
            "score": float(r["score"]),
        })

    confidence = grade_confidence(int(top["sample_size"].sum()))

    note = (f"预算 {budget_min_wan:g}~{budget_max_wan:g} 万元，"
            f"在 {budget_count} 条车源中筛出 {len(grouped)} 个可选车系，展示前 {len(recs)} 个。"
            f"排序综合「热度(样本量) {W_POPULARITY}｜车龄新 {W_NEWNESS}｜里程低 {W_LOW_MILEAGE}」，"
            "均为挂牌价口径，供参考。")

    return {
        "ok": True,
        "tool": "recommend",
        "result_status": "success",
        "budget_min_wan": budget_min_wan,
        "budget_max_wan": budget_max_wan,
        "city": city,
        "brand": brand,
        "series": series,
        "energy_type": "、".join(energy_values) if energy_values else energy_type,
        "filters_applied": filters_applied,
        "budget_count": budget_count,
        "n_candidates": int(len(grouped)),
        "weights": {"popularity": W_POPULARITY, "newness": W_NEWNESS, "low_mileage": W_LOW_MILEAGE},
        "recommendations": recs,
        "confidence": confidence,
        "note": note,
        "data_capabilities": contract_summary(snapshot),
        "dataset_version": snapshot.version,
    }


def _empty(
    budget_max_wan,
    budget_min_wan,
    city,
    brand,
    series,
    energy_type,
    msg,
    budget_count=0,
    filters_applied=None,
    reason_code="no_data",
    result_status="empty",
    observed=None,
    relaxation_candidates=None,
    snapshot=None,
) -> dict:
    return {
        "ok": False,
        "tool": "recommend",
        "result_status": result_status,
        "reason_code": reason_code,
        "budget_min_wan": budget_min_wan,
        "budget_max_wan": budget_max_wan,
        "city": city,
        "brand": brand,
        "series": series,
        "energy_type": energy_type,
        "filters_applied": filters_applied or [],
        "budget_count": budget_count,
        "n_candidates": 0,
        "weights": {"popularity": W_POPULARITY, "newness": W_NEWNESS, "low_mileage": W_LOW_MILEAGE},
        "recommendations": [],
        "confidence": "none",
        "observed": observed or {"budget_count": budget_count},
        "relaxation_candidates": relaxation_candidates or [],
        "note": msg,
        "data_capabilities": contract_summary(snapshot),
        "dataset_version": snapshot.version if snapshot else None,
    }
