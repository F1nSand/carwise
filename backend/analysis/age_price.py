from __future__ import annotations

import hashlib
import json

import numpy as np
import pandas as pd

from .correlation import _correlations
from .statistics import make_result


def _age_scatter(context, points, limit=2000):
    total = len(points)
    chosen = points
    if total > limit:
        parameters = json.dumps(
            context.normalized_filters,
            sort_keys=True,
            ensure_ascii=False,
        )
        seed_material = f"{context.snapshot.version}{parameters}".encode()
        seed = int.from_bytes(hashlib.sha256(seed_material).digest()[:8], "big")
        indexes = np.random.default_rng(seed).choice(total, size=limit, replace=False)
        chosen = points.iloc[np.sort(indexes)]
    return {
        "total_points": total,
        "display_points": len(chosen),
        "sampled": total > len(chosen),
        "points": [
            {"age_years": float(age), "price_yuan": float(price)}
            for age, price in zip(chosen.age_years, chosen.price_yuan)
        ],
    }


def age_price_relation(context, params=None):
    width = (params or {}).get("bin_width_years", 1.0)
    if (
        isinstance(width, bool)
        or not isinstance(width, (int, float))
        or not np.isfinite(width)
        or width < 0.25
        or width > 10
    ):
        raise ValueError("bin_width_years must be finite and in [0.25, 10]")

    frame = context.frame
    required = {"vehicle_age_years", "price_yuan"}
    if not required.issubset(frame.columns):
        return make_result(
            context,
            "age_price_relation",
            {
                "n": 0,
                "missing_age_n": context.scope_rows,
                "correlation": {
                    "pearson_r": None,
                    "spearman_rho": None,
                    "reason": "field_unavailable",
                },
                "bins": [],
                "scatter": {
                    "total_points": 0,
                    "display_points": 0,
                    "sampled": False,
                    "points": [],
                },
            },
            0,
            status="unsupported",
            reason="field_unavailable",
        )

    ages = pd.to_numeric(frame.vehicle_age_years, errors="coerce")
    ages = ages.replace([np.inf, -np.inf], np.nan)
    missing_age_count = int(ages.isna().sum())
    pairs = pd.DataFrame(
        {
            "age": ages,
            "price": pd.to_numeric(frame.price_yuan, errors="coerce"),
        }
    )
    pairs = pairs.replace([np.inf, -np.inf], np.nan).dropna()
    pairs = pairs[(pairs.age >= 0) & (pairs.price > 0)]
    pearson, spearman, reason = _correlations(pairs.age, pairs.price)

    bins = []
    if len(pairs):
        with np.errstate(over="ignore", invalid="ignore"):
            lower = np.floor(float(pairs.age.min()) / width) * width
            upper = np.floor(float(pairs.age.max()) / width) * width
            span = (upper - lower) / width
        if not np.isfinite(span) or span >= 200:
            raise ValueError("bin_width_years creates more than 200 bins")
        edges = np.arange(lower, upper + width * 1.001, width)
        if len(edges) < 2:
            edges = np.array([lower, lower + width])
        if len(edges) > 201:
            raise ValueError("bin_width_years creates more than 200 bins")
        for left, right in zip(edges[:-1], edges[1:]):
            in_bin = pairs.age >= left
            if right == edges[-1]:
                in_bin &= pairs.age <= right
            else:
                in_bin &= pairs.age < right
            group = pairs.loc[in_bin]
            prices = group.price
            enough_samples = len(group) >= 10
            bins.append(
                {
                    "lower_years": float(left),
                    "upper_years": float(right),
                    "n": len(group),
                    "median_price_yuan": float(prices.median()) if enough_samples else None,
                    "p25_price_yuan": float(prices.quantile(0.25)) if enough_samples else None,
                    "p75_price_yuan": float(prices.quantile(0.75)) if enough_samples else None,
                }
            )

    points = pairs.rename(columns={"age": "age_years", "price": "price_yuan"})
    return make_result(
        context,
        "age_price_relation",
        {
            "n": len(pairs),
            "missing_age_n": missing_age_count,
            "correlation": {
                "pearson_r": pearson,
                "spearman_rho": spearman,
                "reason": reason,
            },
            "bins": bins,
            "scatter": _age_scatter(context, points),
        },
        len(pairs),
        context.scope_rows - len(pairs),
        limitations=[
            "车龄是快照记录值，不按当前年份推算",
            "相关不代表因果；不同车系、配置和里程构成可能影响车龄与挂牌价关系",
        ],
        missing=missing_age_count,
    )
