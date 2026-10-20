"""只读检索项目清洗车源数据的工具。"""

from __future__ import annotations

from typing import Any

import pandas as pd

from backend.data_loader import get_dataset_snapshot

_MAX_LIMIT = 50
_DEFAULT_LIMIT = 20
_ROW_COLUMNS = (
    "id",
    "series",
    "brand_name",
    "car_name",
    "city_name",
    "production_year",
    "price_yuan",
    "mileage_km",
    "registration_date",
    "data_quality_score",
)


def _validate_limit(limit: int) -> int:
    if isinstance(limit, bool):
        raise ValueError("limit 必须是整数")
    try:
        value = int(limit)
    except (TypeError, ValueError) as exc:
        raise ValueError("limit 必须是整数") from exc
    if value < 1 or value > _MAX_LIMIT:
        raise ValueError(f"limit 必须在 1~{_MAX_LIMIT} 之间")
    return value


def _clean_value(value: Any) -> Any:
    if pd.isna(value):
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if isinstance(value, float) else int(value)
    return str(value)


def query_csv_dataset(
    series: str | None = None,
    brand: str | None = None,
    city: str | None = None,
    year: int | None = None,
    min_price_wan: float | None = None,
    max_price_wan: float | None = None,
    limit: int = _DEFAULT_LIMIT,
) -> dict[str, Any]:
    """按受控条件查询项目内清洗数据，不接受路径、SQL 或表达式。"""
    limit = _validate_limit(limit)
    if min_price_wan is not None and max_price_wan is not None:
        if float(min_price_wan) > float(max_price_wan):
            raise ValueError("min_price_wan 不能大于 max_price_wan")

    snapshot = get_dataset_snapshot()
    from backend.tools.catalog_resolution import resolve_legacy_arguments
    args, catalog_error = resolve_legacy_arguments(snapshot, "csv_query", {
        "series": series, "brand": brand, "city": city, "year": year,
        "min_price_wan": min_price_wan, "max_price_wan": max_price_wan,
    })
    if catalog_error: return catalog_error
    series, brand, city = args.get("series"), args.get("brand"), args.get("city")
    df = snapshot.dataframe
    sub = df.copy()
    filters_applied: list[str] = []

    def exact_filter(column: str, value: str | None, label: str) -> None:
        nonlocal sub
        if value is None or value == "":
            return
        if column not in sub.columns:
            raise ValueError(f"数据集缺少字段：{column}")
        text = str(value).strip()
        sub = sub[sub[column].astype(str).str.strip() == text]
        filters_applied.append(f"{label} = {text}")

    exact_filter("series", series, "车系")
    exact_filter("brand_name", brand, "品牌")
    exact_filter("city_name", city, "城市")

    if year is not None:
        if "production_year" not in sub.columns:
            raise ValueError("数据集缺少字段：production_year")
        sub = sub[sub["production_year"] == int(year)]
        filters_applied.append(f"生产年份 = {int(year)}")

    if min_price_wan is not None or max_price_wan is not None:
        if "price_yuan" not in sub.columns:
            raise ValueError("数据集缺少字段：price_yuan")
        low = float(min_price_wan) * 10000 if min_price_wan is not None else float("-inf")
        high = float(max_price_wan) * 10000 if max_price_wan is not None else float("inf")
        sub = sub[(sub["price_yuan"] >= low) & (sub["price_yuan"] <= high)]
        filters_applied.append(
            f"挂牌价在 {min_price_wan if min_price_wan is not None else '-'}~"
            f"{max_price_wan if max_price_wan is not None else '-'} 万元"
        )

    match_count = int(len(sub))
    if match_count == 0:
        relaxation_candidates: list[dict[str, Any]] = []
        if city:
            relaxation_candidates.append({"field": "city", "action": "remove", "auto": True, "label": "去掉城市限制"})
        if year is not None:
            relaxation_candidates.append({"field": "year", "action": "remove", "auto": True, "label": "放宽生产年份"})
        if min_price_wan is not None or max_price_wan is not None:
            relaxation_candidates.append({"field": "price_range", "action": "widen", "auto": False, "label": "放宽价格范围"})
        if series:
            relaxation_candidates.append({"field": "series", "action": "remove", "auto": False, "label": "去掉车系限制"})
        return {
            "ok": False,
            "tool": "csv_query",
            "result_status": "empty",
            "reason_code": "no_matching_rows",
            "filters_applied": filters_applied,
            "match_count": 0,
            "rows": [],
            "columns": [],
            "observed": {"match_count": 0, "dataset_rows": int(len(df))},
            "relaxation_candidates": relaxation_candidates,
            "note": "没有找到符合条件的车源样本。",
            "dataset_version": snapshot.version,
        }

    available = [column for column in _ROW_COLUMNS if column in sub.columns]
    rows: list[dict[str, Any]] = []
    for _, row in sub.head(limit).iterrows():
        item = {column: _clean_value(row[column]) for column in available}
        if "price_yuan" in item and item["price_yuan"] is not None:
            item["price_wan"] = round(float(item["price_yuan"]) / 10000, 2)
        rows.append(item)

    note = f"共命中 {match_count} 条车源，展示前 {len(rows)} 条。"
    if match_count > limit:
        note += f"结果已按 limit={limit} 截断。"
    return {
        "ok": True,
        "tool": "csv_query",
        "result_status": "success",
        "filters_applied": filters_applied,
        "match_count": match_count,
        "rows": rows,
        "columns": available + (["price_wan"] if "price_yuan" in available else []),
        "note": note,
        "dataset_version": snapshot.version,
    }
