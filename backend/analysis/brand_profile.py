from __future__ import annotations

from .statistics import make_result, numeric, quantiles


def brand_profile(context, params=None):
    top_k = (params or {}).get("top_k", 10)
    if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= 20:
        raise ValueError("top_k must be an integer from 1 to 20")

    frame = context.frame
    denominator = len(frame)
    if "brand_name" not in frame:
        return make_result(
            context,
            "brand_profile",
            {"denominator_n": denominator, "groups": []},
            0,
            status="unsupported",
            reason="brand_field_unavailable",
        )

    brands = frame.brand_name.fillna("").astype(str).str.strip()
    sizes = brands.value_counts()
    named = [(str(name), int(count)) for name, count in sizes.items() if name]
    named.sort(key=lambda item: (-item[1], item[0]))
    groups = []

    for brand, count in named[:top_k]:
        group = frame.loc[brands.eq(brand)]
        price = quantiles(group["price_yuan"]) if "price_yuan" in group else quantiles([])
        mileage = numeric(group["mileage_km"]) if "mileage_km" in group else numeric([])
        age = numeric(group["vehicle_age_years"]) if "vehicle_age_years" in group else numeric([])
        groups.append(
            {
                "brand_name": brand,
                "n": count,
                "share": count / denominator if denominator else None,
                "price": {
                    "n": price["n"],
                    "min": price["min"],
                    "max": price["max"],
                    "median": price["median"],
                    "p25": price["p25"],
                    "p75": price["p75"],
                },
                "series_count": int(group["series"].nunique()) if "series" in group else 0,
                "mileage": {
                    "n": len(mileage),
                    "median_km": float(mileage.median()) if len(mileage) else None,
                },
                "age": {
                    "n": len(age),
                    "median_years": float(age.median()) if len(age) else None,
                },
                "category": "brand",
            }
        )

    other_count = sum(count for _, count in named[top_k:])
    unknown_count = int(sizes.get("", 0))
    for label, count, category in (
        ("其他", other_count, "other"),
        ("未知", unknown_count, "unknown"),
    ):
        if not count:
            continue
        groups.append(
            {
                "brand_name": label,
                "n": count,
                "share": count / denominator if denominator else None,
                "price": {
                    "n": 0,
                    "min": None,
                    "max": None,
                    "median": None,
                    "p25": None,
                    "p75": None,
                },
                "series_count": 0,
                "mileage": {"n": 0, "median_km": None},
                "age": {"n": 0, "median_years": None},
                "category": category,
            }
        )

    return make_result(
        context,
        "brand_profile",
        {"denominator_n": denominator, "groups": groups},
        denominator,
        limitations=["占比仅以当前筛选样本为分母，不代表全国市场份额"],
    )
