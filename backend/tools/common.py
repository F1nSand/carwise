# -*- coding: utf-8 -*-
"""
分析工具共用模块

提供各工具（同款比价、区域价差…）共用的：
  - wan(v)                  元 -> 万（保留 2 位）
  - apply_global_filter(df) 全局异常过滤（价格/里程/年份）
  - iqr_ratio(prices)       价格离散度 = IQR / 中位数
  - grade_confidence(n, iqr_ratio)  可信度分级（样本量 + 离散度）

可信度分级规则（可写进论文，非黑盒）：
  基础档位按样本量：n>=30 high / 10~29 medium / <10 low
  离散度修正（IQR/中位）：
     >= 1.0  降两级（价格极分散，点估计不可靠）
     0.5~1.0 降一级（波动明显）
     < 0.5   不降级（价格集中）
  最终档位映射：3=high / 2=medium / 1=low
  该规则与 docs/07「可靠性」定义一致：低可靠 = 样本少、离散大或字段不完整。
"""
from __future__ import annotations

import pandas as pd

from backend.config import TH


def wan(v: float | None) -> float | None:
    """元 -> 万（保留 2 位）。"""
    if v is None or pd.isna(v):
        return None
    return round(float(v) / 10000.0, 2)


def apply_global_filter(df: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """
    全局异常过滤：0 < price_yuan < 阈值；mileage_km < 阈值；年份在 [year_min, year_max]。
    返回 (过滤后子集, 已应用条件说明列表)。
    """
    filters_applied: list[str] = []

    mask = (
        df["price_yuan"].notna()
        & (df["price_yuan"] > TH["price_min_yuan"])
        & (df["price_yuan"] < TH["price_max_yuan"])
    )
    sub = df[mask].copy()
    filters_applied.append(
        f"价格在 ({TH['price_min_yuan']}, {TH['price_max_yuan']}) 元区间内"
    )

    if "mileage_km" in sub.columns:
        sub = sub[sub["mileage_km"].isna() | (sub["mileage_km"] < TH["mileage_max_km"])]

    if "production_year" in sub.columns:
        sub = sub[
            sub["production_year"].isna()
            | ((sub["production_year"] >= TH["year_min"])
               & (sub["production_year"] <= TH["year_max"]))
        ]

    return sub, filters_applied


def iqr_ratio(prices: pd.Series) -> float | None:
    """价格离散度 = IQR / 中位数。空集或中位<=0 返回 None（防除零）。"""
    if prices is None or len(prices) == 0:
        return None
    q1 = float(prices.quantile(0.25))
    q3 = float(prices.quantile(0.75))
    med = float(prices.median())
    if med <= 0:
        return None
    return (q3 - q1) / med


_LEVEL_NAME = {3: "high", 2: "medium", 1: "low"}


def grade_confidence(n: int, iqr_ratio: float | None = None) -> str:
    """
    融合 样本量 + 价格离散度 的可信度分级。

    参数：
      n         样本量
      iqr_ratio 离散度 IQR/中位（None 时只看样本量）
    返回："high" | "medium" | "low"
    """
    if n >= 30:
        level = 3
    elif n >= 10:
        level = 2
    else:
        level = 1

    if iqr_ratio is not None:
        if iqr_ratio >= 1.0:
            level -= 2        # 价格极分散
        elif iqr_ratio >= 0.5:
            level -= 1        # 波动明显

    level = max(1, min(3, level))
    return _LEVEL_NAME[level]
