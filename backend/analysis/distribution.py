from __future__ import annotations

import numpy as np

from .statistics import make_result, numeric, quantiles


def price_distribution(context, params=None):
    bin_count = (params or {}).get("bins", 20)
    if isinstance(bin_count, bool) or not isinstance(bin_count, int) or not 5 <= bin_count <= 40:
        raise ValueError("bins must be an integer from 5 to 40")
    if "price_yuan" not in context.frame:
        return make_result(
            context,
            "price_distribution",
            {},
            0,
            status="unsupported",
            reason="price_field_unavailable",
        )

    prices = numeric(context.frame["price_yuan"])
    summary = quantiles(prices)
    bins = []
    if len(prices):
        minimum = float(prices.min())
        maximum = float(prices.max())
        if minimum == maximum:
            edges = np.array([minimum - 0.5, maximum + 0.5])
        else:
            edges = np.linspace(minimum, maximum, bin_count + 1)
        counts = np.histogram(prices.to_numpy(), bins=edges)[0]
        for index, count in enumerate(counts):
            bins.append(
                {
                    "lower_yuan": float(edges[index]),
                    "upper_yuan": float(edges[index + 1]),
                    "count": int(count),
                    "share": float(count / len(prices)),
                    "include_upper": index == len(counts) - 1,
                }
            )

    data = {
        "n": summary["n"],
        "min_yuan": summary["min"],
        "max_yuan": summary["max"],
        "mean_yuan": summary["mean"],
        "median_yuan": summary["median"],
        "p25_yuan": summary["p25"],
        "p75_yuan": summary["p75"],
        "bins": bins,
    }
    return make_result(
        context,
        "price_distribution",
        data,
        len(prices),
        max(0, context.scope_rows - len(prices)),
        methodology={
            "price_basis": "挂牌价",
            "quantile_interpolation": "linear",
            "binning": "equal_width",
        },
    )
