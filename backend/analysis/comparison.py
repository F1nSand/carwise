from __future__ import annotations

from .statistics import make_result, numeric, quantiles


def model_comparison(context, params=None):
    selectors = context.normalized_filters["series_selectors"]
    if len(selectors) < 2:
        return make_result(
            context,
            "model_comparison",
            {"groups": []},
            0,
            status="unsupported",
            reason="select_at_least_two_series",
            limitations=["至少选择两个品牌与车系组合"],
        )

    frame = context.frame
    groups = []
    for selector in selectors:
        if "series" in frame and "brand_name" in frame:
            group = frame[
                (frame.brand_name.astype(str) == selector["brand_name"])
                & (frame.series.astype(str) == selector["series"])
            ]
        else:
            group = frame.iloc[0:0]

        price = quantiles(group["price_yuan"]) if "price_yuan" in group else quantiles([])
        mileage = numeric(group["mileage_km"]) if "mileage_km" in group else numeric([])
        age = numeric(group["vehicle_age_years"]) if "vehicle_age_years" in group else numeric([])
        count = len(group)
        groups.append(
            {
                "brand_name": selector["brand_name"],
                "series": selector["series"],
                "status": "success" if count else "empty",
                "n": count,
                "price": {
                    "n": price["n"],
                    "min_yuan": price["min"],
                    "max_yuan": price["max"],
                    "median_yuan": price["median"],
                    "p25_yuan": price["p25"],
                    "p75_yuan": price["p75"],
                },
                "mileage": {
                    "n": len(mileage),
                    "median_km": float(mileage.median()) if len(mileage) else None,
                },
                "age": {
                    "n": len(age),
                    "median_years": float(age.median()) if len(age) else None,
                },
                "support": {"low_sample": 0 < count < 10},
            }
        )

    return make_result(
        context,
        "model_comparison",
        {"groups": groups},
        sum(group["n"] for group in groups),
        limitations=["车系汇总不代表配置完全相同车辆的比较"],
    )
