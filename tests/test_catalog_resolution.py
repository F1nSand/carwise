import pandas as pd

from backend.data_loader import DatasetSnapshot
from backend.tools.catalog_resolution import resolve_keyword, resolve_tool_filters


def snapshot(rows):
    frame = pd.DataFrame(rows, columns=["brand_name", "series", "city_name"])
    return DatasetSnapshot("fixture", frame, {}, [])


def test_exact_series_wins_and_brand_alias_is_scoped():
    data = snapshot([("大众", "朗逸", "北京"), ("奥迪", "大众朗逸", "上海")])
    resolved, error = resolve_keyword(data, "series", "大众朗逸", {}, "t")
    assert error is None and resolved == {"brand_name": "奥迪", "series": "大众朗逸"}
    resolved, error = resolve_keyword(data, "series", "大众朗逸", {"brands": ["大众"]}, "t")
    assert error is None and resolved["series"] == "朗逸"


def test_nfkc_and_whitespace_only_resolve_unique_alias():
    data = snapshot([("品牌", "品牌 A4", "北京")])
    resolved, error = resolve_keyword(data, "series", "品牌A4", {}, "t")
    assert error is None and resolved["series"] == "品牌 A4"
    _, error = resolve_keyword(data, "city", "", {}, "t")
    assert error["reason_code"] == "unknown_city"


def test_ambiguous_and_unknown_carry_filter_context():
    data = snapshot([("甲", "朗逸", "北京"), ("乙", "朗逸", "上海")])
    filters, error = resolve_tool_filters(data, {"brands": ["甲"], "year_min": 2020}, "t")
    assert error is None and filters["brands"] == ["甲"] and filters["year_min"] == 2020
    _, error = resolve_keyword(data, "series", "朗逸", {}, "t")
    assert error["reason_code"] == "ambiguous_series"
    assert len(error["candidate_selectors"]) == 2
    _, error = resolve_keyword(data, "brand", "不存在", {}, "t")
    assert error["reason_code"] == "unknown_brand" and error["normalized_filters"]["year_min"] is None


def test_city_suffix_is_a_unique_city_alias_only():
    data = snapshot([("宝马", "宝马3系", "北京")])
    city, error = resolve_keyword(data, "city", "北京市", {}, "t")
    assert error is None and city == "北京"
    _, error = resolve_keyword(data, "brand", "宝马市", {}, "t")
    assert error["reason_code"] == "unknown_brand"


def test_legacy_adapter_preserves_all_filters_on_keyword_error():
    from backend.tools.catalog_resolution import resolve_legacy_arguments

    data = snapshot([("DS", "DS 7", "北京")])
    _, error = resolve_legacy_arguments(data, "price_compare", {
        "series": "missing", "brand": " DS ", "city": "北京市", "year": 2022,
    })
    assert error["reason_code"] == "unknown_series"
    assert error["requested_arguments"]["brand"] == " DS "
    assert error["normalized_filters"]["brands"] == ["DS"]
    assert error["normalized_filters"]["cities"] == ["北京"]
    assert error["normalized_filters"]["year_min"] == error["normalized_filters"]["year_max"] == 2022
    assert error["normalized_filters"]["series_selectors"] == [{"brand_name": "DS", "series": "missing"}]


def test_legacy_adapter_canonical_brand_binds_series_identity():
    from backend.tools.catalog_resolution import resolve_legacy_arguments

    data = snapshot([("DS", "DS 7", "北京"), ("甲", "同名", "北京"), ("乙", "同名", "北京")])
    args, error = resolve_legacy_arguments(data, "price_compare", {"series": "7", "brand": " ｄｓ "})
    assert error is None
    assert args["brand"] == "DS" and args["series"] == "DS 7"
    _, error = resolve_legacy_arguments(data, "price_compare", {"series": "同名"})
    assert error["reason_code"] == "ambiguous_series"
    assert len(error["candidate_selectors"]) == 2
