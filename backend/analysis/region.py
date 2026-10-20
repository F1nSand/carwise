from __future__ import annotations

from .statistics import make_result, numeric


def compute_city_stats(valid, minimum):
    """Shared city-price core. Input rows have price_yuan and a normalized _city."""
    baseline = float(valid.price_yuan.median()) if len(valid) else None
    rows = []
    for city, group in valid.groupby("_city", sort=True):
        prices = numeric(group.price_yuan)
        median = float(prices.median()) if len(prices) else None
        count = len(group)
        rows.append(
            {
                "city": str(city),
                "n": count,
                "median_yuan": median,
                "p25_yuan": float(prices.quantile(0.25)) if len(prices) else None,
                "p75_yuan": float(prices.quantile(0.75)) if len(prices) else None,
                "diff_yuan": median - baseline if median is not None and baseline is not None else None,
                "diff_pct": (
                    (median - baseline) * 100 / baseline
                    if median is not None and baseline
                    else None
                ),
                "eligible": count >= minimum,
                "low_sample": count < minimum,
            }
        )
    rows.sort(
        key=lambda row: (
            not row["eligible"],
            row["median_yuan"] if row["median_yuan"] is not None else float("inf"),
            row["city"],
        )
    )
    return baseline, rows


def region_gap(context, params=None):
    minimum = (params or {}).get("min_city_n", 15)
    if isinstance(minimum, bool) or not isinstance(minimum, int) or not 1 <= minimum <= 10000:
        raise ValueError("min_city_n must be an integer from 1 to 10000")

    frame = context.frame
    if "city_name" not in frame:
        return make_result(
            context,
            "region_gap",
            {
                "baseline_median_yuan": None,
                "baseline_n": 0,
                "baseline_label": "filtered_sample",
                "valid_city_count": 0,
                "cities": [],
            },
            0,
            status="unsupported",
            reason="city_field_unavailable",
        )

    if "price_yuan" in frame:
        valid = frame[
            frame.city_name.notna()
            & frame.city_name.astype(str).str.strip().ne("")
            & frame.price_yuan.notna()
        ].copy()
    else:
        valid = frame.iloc[0:0].copy()
    valid["_city"] = valid.city_name.astype(str).str.strip()
    baseline, rows = compute_city_stats(valid, minimum)
    result = make_result(
        context,
        "region_gap",
        {
            "baseline_median_yuan": baseline,
            "baseline_n": len(valid),
            "baseline_label": (
                "selected_city_sample"
                if context.normalized_filters["cities"]
                else "filtered_sample"
            ),
            "valid_city_count": sum(row["eligible"] for row in rows),
            "cities": rows,
        },
        len(valid),
        max(0, context.scope_rows - len(valid)),
        limitations=[
            "挂牌价地区差异受车系、车龄等样本构成影响，不能解释为城市导致价格变化"
        ],
    )
    if len(rows) == 1:
        result["limitations"].append("仅一个城市，没有跨城市价差结论")
    return result
