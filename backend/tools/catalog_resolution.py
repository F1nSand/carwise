from __future__ import annotations

import unicodedata
import threading
import weakref
from collections import OrderedDict
from typing import Any

from backend.analysis.contracts import normalize_filters

_FIELD = {
    "brand": ("brand_name", "品牌名称", "品牌"),
    "series": ("series", "车系"),
    "city": ("city_name", "车源城市名称", "城市"),
}
_CATALOG_CACHE = OrderedDict()
_CACHE_LOCK = threading.RLock()
_CACHE_FRAMES: dict[tuple, weakref.ReferenceType] = {}

_REASON = {"brand": ("ambiguous_brand", "unknown_brand"), "series": ("ambiguous_series", "unknown_series"), "city": ("ambiguous_city", "unknown_city")}


def _norm(value: Any) -> str:
    return "".join(unicodedata.normalize("NFKC", str(value)).split()).casefold()


def _values(snapshot, field: str) -> list[str]:
    cache_key = (snapshot.version, id(snapshot.dataframe), field)
    with _CACHE_LOCK:
        if cache_key in _CATALOG_CACHE and (_CACHE_FRAMES.get(cache_key) and _CACHE_FRAMES[cache_key]() is snapshot.dataframe):
            _CATALOG_CACHE.move_to_end(cache_key)
            return _CATALOG_CACHE[cache_key]
    df = snapshot.dataframe
    col = next((name for name in _FIELD[field] if name in df.columns), None)
    if col is None:
        return []
    result = sorted({str(v).strip() for v in df[col].dropna().tolist() if str(v).strip()})
    with _CACHE_LOCK:
        _CATALOG_CACHE[cache_key] = result
        _CACHE_FRAMES[cache_key] = weakref.ref(snapshot.dataframe)
        while len(_CATALOG_CACHE) > 8:
            old, _ = _CATALOG_CACHE.popitem(last=False)
            _CACHE_FRAMES.pop(old, None)
    return result


def _identities(snapshot) -> list[dict[str, str]]:
    cache_key = (snapshot.version, id(snapshot.dataframe), "identities")
    with _CACHE_LOCK:
        if cache_key in _CATALOG_CACHE and (_CACHE_FRAMES.get(cache_key) and _CACHE_FRAMES[cache_key]() is snapshot.dataframe):
            _CATALOG_CACHE.move_to_end(cache_key)
            return _CATALOG_CACHE[cache_key]
    df = snapshot.dataframe
    brand_col = next((x for x in _FIELD["brand"] if x in df.columns), None)
    series_col = next((x for x in _FIELD["series"] if x in df.columns), None)
    if not brand_col or not series_col:
        return []
    result = [{"brand_name": b, "series": series} for b, series in sorted({(str(b).strip(), str(series).strip())
                   for b, series in df[[brand_col, series_col]].dropna().itertuples(index=False, name=None)
                   if str(b).strip() and str(series).strip()})]
    with _CACHE_LOCK:
        _CATALOG_CACHE[cache_key] = result
        _CACHE_FRAMES[cache_key] = weakref.ref(snapshot.dataframe)
        while len(_CATALOG_CACHE) > 8:
            old, _ = _CATALOG_CACHE.popitem(last=False)
            _CACHE_FRAMES.pop(old, None)
    return result


def _match(value: str, candidates: list[str], field: str = "") -> tuple[str | None, list[str]]:
    if value in candidates:
        return value, [value]
    key = _norm(value)
    if not key:
        return None, []
    matches = [candidate for candidate in candidates if _norm(candidate) == key]
    if not matches and field == "city":
        city_key = key[:-1] if key.endswith("市") else key + "市"
        if city_key:
            matches = [candidate for candidate in candidates if _norm(candidate) == city_key]
    if len(matches) == 1:
        return matches[0], matches
    return None, matches


def resolve_keyword(snapshot, field: str, value: str, filters: dict | None = None, tool: str = "") -> tuple[Any, dict | None]:
    """Resolve one brand/series/city against the same immutable dataset snapshot."""
    field = {"brand_name": "brand", "brands": "brand", "series_selectors": "series", "city_name": "city", "cities": "city"}.get(field, field)
    if field not in _FIELD:
        raise ValueError(f"unsupported catalog field: {field}")
    normalized = normalize_filters(filters)
    if not _norm(value):
        reason = _REASON[field][1]
        error = {"ok": False, "tool": tool, "result_status": "error", "reason_code": reason,
                 "keyword_field": field, "requested_keyword": value, "candidate_values": [],
                 "candidate_selectors": [], "normalized_filters": normalized,
                 "dataset_version": snapshot.version, "repair_hint": "名称不能为空，请提供一个真实目录名称。"}
        if field == "series": error["requested_series"] = value
        return None, error
    identities = _identities(snapshot)
    if field == "series":
        brands = normalized["brands"]
        candidates = [x for x in identities if not brands or x["brand_name"] in brands]
        exact = [x for x in identities if x["series"] == value and (not brands or x["brand_name"] in brands)]
        if exact:
            matches = exact
        else:
            key = _norm(value)
            matches = [x for x in candidates if _norm(x["series"]) == key]
            if not matches:
                for identity in candidates:
                    bkey = _norm(identity["brand_name"])
                    series_key = _norm(identity["series"])
                    # Permit a brand-scoped alias both when a canonical name
                    # includes the brand prefix and when the dataset omits it.
                    aliases = {series_key}
                    if series_key.startswith(bkey):
                        aliases.add(series_key[len(bkey):])
                    else:
                        aliases.add(bkey + series_key)
                    if key in aliases:
                        matches.append(identity)
        if len(matches) == 1:
            return matches[0], None
        reason = "ambiguous_series" if matches else "unknown_series"
        candidates_out = list(dict.fromkeys(x["series"] for x in matches))
        # Prefix families are suggestions only.
        if not matches:
            candidates_out = sorted({x["series"] for x in candidates if _norm(x["series"]).startswith(_norm(value))})[:20]
            if candidates_out:
                reason = "ambiguous_series"
        selectors = matches or [x for x in candidates if x["series"] in candidates_out]
    else:
        all_values = _values(snapshot, field)
        candidates_out = all_values
        resolved, matches = _match(value, candidates_out, field)
        if resolved:
            return resolved, None
        reason = _REASON[field][0] if matches else _REASON[field][1]
        if matches:
            candidates_out = matches
        if not matches:
            prefix = _norm(value)
            candidates_out = [v for v in candidates_out if _norm(v).startswith(prefix)][:20]
            if candidates_out:
                reason = _REASON[field][0]
        selectors = []
    error = {"ok": False, "tool": tool, "result_status": "error", "reason_code": reason,
             "keyword_field": field, "requested_keyword": value,
             "candidate_values": candidates_out,
             "candidate_selectors": [{"brand_name": x["brand_name"], "series": x["series"]} for x in selectors],
             "normalized_filters": normalized, "dataset_version": snapshot.version,
             "repair_hint": "请通过 ask_user 确认一个真实目录名称后继续，并保留其他筛选条件。"}
    if field == "series":
        error["requested_series"] = value
    return None, error



def resolve_legacy_arguments(snapshot, tool: str, arguments: dict[str, Any]) -> tuple[dict[str, Any], dict | None]:
    """Resolve legacy tool keywords while retaining every supplied filter on errors."""
    args = dict(arguments)
    original = dict(arguments)
    brand_value, series_value, city_value = args.get("brand"), args.get("series"), args.get("city")
    normalized = {"brands": [], "series_selectors": [], "cities": [], "energy_types": []}
    for key in ("price_min_yuan", "price_max_yuan", "mileage_min_km", "mileage_max_km", "year_min", "year_max", "age_min_years", "age_max_years"):
        if key in args and args[key] is not None: normalized[key] = args[key]
    for low_key, high_key, dst_low, dst_high in (("min_price_wan", "max_price_wan", "price_min_yuan", "price_max_yuan"), ("budget_min_wan", "budget_max_wan", "price_min_yuan", "price_max_yuan")):
        if args.get(low_key) is not None: normalized[dst_low] = float(args[low_key]) * 10000
        if args.get(high_key) is not None: normalized[dst_high] = float(args[high_key]) * 10000
    if args.get("year") is not None: normalized["year_min"] = normalized["year_max"] = int(args["year"])
    if args.get("max_age") is not None: normalized["age_max_years"] = int(args["max_age"])
    if brand_value: normalized["brands"] = [str(brand_value)]
    if city_value: normalized["cities"] = [str(city_value)]
    if args.get("energy_type"): normalized["energy_types"] = [str(args["energy_type"])]
    error = None
    canonical_brand = None
    if brand_value:
        canonical_brand, error = resolve_keyword(snapshot, "brand", brand_value, normalized, tool)
    if series_value is not None and canonical_brand:
        normalized["series_selectors"] = [{"brand_name": canonical_brand, "series": str(series_value)}]
    canonical_city = None
    if not error and city_value:
        canonical_city, error = resolve_keyword(snapshot, "city", city_value, normalized, tool)
    identity = None
    if not error and series_value is not None:
        scope = [canonical_brand] if canonical_brand else []
        identity, error = resolve_keyword(snapshot, "series", series_value, {**normalized, "brands": scope}, tool)
        if not error:
            canonical_brand = identity["brand_name"]
            normalized["series_selectors"] = [identity]
    if not error and normalized["energy_types"]:
        from backend.tools.data_contract import normalize_energy_filter
        energy_values, energy_error = normalize_energy_filter(normalized["energy_types"][0], snapshot)
        if energy_error:
            error = {"ok": False, "tool": tool, "result_status": "error", "reason_code": "unsupported_filter", "error_code": "unsupported_filter", "keyword_field": "energy_type", "requested_keyword": args.get("energy_type"), "candidate_values": [], "candidate_selectors": [], "dataset_version": snapshot.version, "error": energy_error, "note": energy_error}
        else:
            normalized["energy_types"] = sorted(set(energy_values))
    if identity:
        normalized["series_selectors"] = [identity]
    if canonical_brand:
        normalized["brands"] = [canonical_brand]
    if canonical_city:
        normalized["cities"] = [canonical_city]
    if error:
        error["normalized_filters"] = normalized
        error["dataset_version"] = snapshot.version
        error["requested_arguments"] = original
        if error.get("keyword_field") == "series": error["requested_series"] = series_value
        return args, error
    if identity: args["series"] = identity["series"]
    if canonical_brand: args["brand"] = canonical_brand
    if canonical_city: args["city"] = canonical_city
    args["normalized_filters"] = normalized
    return args, None

def resolve_tool_filters(snapshot, filters: dict | None = None, tool: str = "") -> tuple[dict, dict | None]:
    """Normalize catalog-backed filters against a single dataset snapshot."""
    normalized = normalize_filters(filters)
    for field, key in (("brand", "brands"),):
        resolved_values = []
        for value in normalized[key]:
            resolved, error = resolve_keyword(snapshot, field, value, normalized, tool)
            if error:
                error["normalized_filters"] = normalized
                return normalized, error
            resolved_values.append(resolved)
        normalized[key] = sorted(set(resolved_values))
    selectors = []
    for selector in normalized["series_selectors"]:
        brand, error = resolve_keyword(snapshot, "brand", selector["brand_name"], normalized, tool)
        if error:
            error["normalized_filters"] = normalized
            return normalized, error
        if normalized["brands"] and brand not in normalized["brands"]:
            return normalized, {"ok": False, "tool": tool, "result_status": "unsupported", "reason_code": "incompatible_filters", "error_code": "unsupported_filter", "keyword_field": "series", "requested_keyword": selector["series"], "candidate_values": [], "candidate_selectors": [], "normalized_filters": normalized, "dataset_version": snapshot.version, "repair_hint": "车系品牌与 brands 条件不一致，请确认联合身份。"}
        series, error = resolve_keyword(snapshot, "series", selector["series"], {**normalized, "brands": [brand]}, tool)
        if error:
            error["normalized_filters"] = normalized
            return normalized, error
        if series["brand_name"] != brand:
            return normalized, {"ok": False, "tool": tool, "result_status": "error", "reason_code": "ambiguous_series", "keyword_field": "series", "requested_keyword": selector["series"], "candidate_values": [series["series"]], "candidate_selectors": [series], "normalized_filters": normalized, "dataset_version": snapshot.version, "repair_hint": "请确认品牌与车系的联合身份。"}
        selectors.append(series)
    normalized["series_selectors"] = selectors
    resolved_cities = []
    for value in normalized["cities"]:
        resolved, error = resolve_keyword(snapshot, "city", value, normalized, tool)
        if error:
            error["normalized_filters"] = normalized
            return normalized, error
        resolved_cities.append(resolved)
    normalized["cities"] = sorted(set(resolved_cities))
    if normalized["energy_types"]:
        from backend.tools.data_contract import normalize_energy_filter
        energy_values = []
        for requested in normalized["energy_types"]:
            values, energy_error = normalize_energy_filter(requested, snapshot)
            if energy_error:
                return normalized, {"ok": False, "tool": tool, "result_status": "unsupported", "reason_code": "unsupported_filter", "error_code": "unsupported_filter", "keyword_field": "energy_type", "requested_keyword": requested, "candidate_values": [], "candidate_selectors": [], "normalized_filters": normalized, "dataset_version": snapshot.version, "error": energy_error, "note": energy_error}
            energy_values.extend(values)
        normalized["energy_types"] = sorted(set(energy_values))
    return normalized, None
