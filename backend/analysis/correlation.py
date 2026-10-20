from __future__ import annotations

import hashlib
import json

import numpy as np
import pandas as pd

from .statistics import make_result


def _correlations(x: pd.Series, y: pd.Series):
    if len(x) < 10:
        return None, None, "insufficient_samples"
    if x.nunique() < 2 or y.nunique() < 2:
        return None, None, "constant_variable"
    pearson = float(x.corr(y, method="pearson"))
    average_x = x.rank(method="average")
    average_y = y.rank(method="average")
    spearman = float(average_x.corr(average_y, method="pearson"))
    return pearson, spearman, None


def _stable_scatter(context, pairs, limit=2000):
    total = len(pairs)
    chosen = pairs
    if total > limit:
        parameters = json.dumps(
            context.normalized_filters,
            sort_keys=True,
            ensure_ascii=False,
        )
        seed_material = f"{context.snapshot.version}{parameters}".encode()
        seed = int.from_bytes(hashlib.sha256(seed_material).digest()[:8], "big")
        indexes = np.random.default_rng(seed).choice(total, size=limit, replace=False)
        chosen = pairs.iloc[np.sort(indexes)]
    points = [
        {"mileage_km": float(mileage), "price_yuan": float(price)}
        for mileage, price in zip(chosen.iloc[:, 0], chosen.iloc[:, 1])
    ]
    return {
        "total_points": total,
        "display_points": len(chosen),
        "sampled": total > len(chosen),
        "points": points,
    }


def mileage_price_correlation(context, params=None):
    frame = context.frame
    required = {"mileage_km", "price_yuan"}
    if not required.issubset(frame.columns):
        return make_result(
            context,
            "mileage_price_correlation",
            {
                "n": 0,
                "pearson_r": None,
                "spearman_rho": None,
                "correlation_reason": "field_unavailable",
                "mileage_bins": [],
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

    pairs = frame[["mileage_km", "price_yuan"]].apply(pd.to_numeric, errors="coerce")
    pairs = pairs.replace([np.inf, -np.inf], np.nan).dropna()
    pairs = pairs[(pairs.mileage_km >= 0) & (pairs.price_yuan > 0)]
    pearson, spearman, reason = _correlations(pairs.mileage_km, pairs.price_yuan)
    bins = []
    if len(pairs):
        minimum = float(pairs.mileage_km.min())
        maximum = float(pairs.mileage_km.max())
        count = min(10, max(1, int(np.sqrt(len(pairs)))))
        if maximum > minimum:
            edges = np.linspace(minimum, maximum, count + 1)
        else:
            edges = np.array([minimum - 0.5, maximum + 0.5])
        indexes = np.digitize(pairs.mileage_km, edges[1:-1])
        for index in range(len(edges) - 1):
            group = pairs.loc[indexes == index]
            prices = group.price_yuan
            bins.append(
                {
                    "lower_km": float(edges[index]),
                    "upper_km": float(edges[index + 1]),
                    "n": len(group),
                    "median_price_yuan": float(prices.median()) if len(group) else None,
                    "p25_price_yuan": float(prices.quantile(0.25)) if len(group) else None,
                    "p75_price_yuan": float(prices.quantile(0.75)) if len(group) else None,
                }
            )

    missing = int(frame[["mileage_km", "price_yuan"]].isna().any(axis=1).sum())
    return make_result(
        context,
        "mileage_price_correlation",
        {
            "n": len(pairs),
            "pearson_r": pearson,
            "spearman_rho": spearman,
            "correlation_reason": reason,
            "mileage_bins": bins,
            "scatter": _stable_scatter(context, pairs),
        },
        len(pairs),
        context.scope_rows - len(pairs),
        limitations=["相关不代表因果；车型构成与车龄可能影响关系"],
        methodology={
            "paired_observations": True,
            "spearman": "pearson_correlation_of_average_ranks",
            "p_value": False,
        },
        missing=missing,
    )
