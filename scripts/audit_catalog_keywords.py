"""Offline exhaustive checks of the current snapshot's catalog resolution contract."""
from __future__ import annotations

import argparse
from collections import defaultdict
import json
from pathlib import Path
import sys
import unicodedata

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.data_loader import get_dataset_snapshot
from backend.tools.catalog_resolution import _identities, _values, resolve_keyword
from backend.tools.data_contract import ENERGY_ALIASES, normalize_energy_filter


def key(value):
    return "".join(unicodedata.normalize("NFKC", value).split()).casefold()


def series_aliases(brand, series):
    name, prefix = key(series), key(brand)
    alias = name[len(prefix):] if name.startswith(prefix) else prefix + name
    return {alias} - {""}


def variants(value):
    fullwidth = "".join(chr(ord(c) + 0xFEE0) if "!" <= c <= "~" else c for c in value)
    return {value, key(value), value.upper(), fullwidth, " " + value + " "}


def audit(snapshot):
    identities = _identities(snapshot)
    by_name, by_normalized, by_alias = (defaultdict(set) for _ in range(3))
    for item in identities:
        identity = (item["brand_name"], item["series"])
        by_name[identity[1]].add(identity)
        by_normalized[key(identity[1])].add(identity)
        for alias in series_aliases(*identity):
            by_alias[alias].add(identity)
    report = {
        "dataset_version": snapshot.version, "row_count": len(snapshot.dataframe),
        "counts": {}, "checks": [], "resolution_failures": [],
        "series_identities": identities, "fields": {},
        "alias_collisions": [{"alias": a, "identities": sorted(rows)}
                             for a, rows in sorted(by_alias.items()) if len(rows) > 1],
        "normalized_name_collisions": [{"name": a, "identities": sorted(rows)}
                                      for a, rows in sorted(by_normalized.items()) if len(rows) > 1],
        "shadowed_aliases": [{"alias": a, "alias_identities": sorted(rows),
                              "canonical_identities": sorted(by_normalized[a])}
                             for a, rows in sorted(by_alias.items())
                             if a in by_normalized and rows != by_normalized[a]],
    }

    def record(field, token, scope, expected, resolved, error):
        actual = ((resolved.get("brand_name"), resolved.get("series"))
                  if isinstance(resolved, dict) else resolved)
        expected = set(expected)
        candidates = ({(x["brand_name"], x["series"]) for x in error.get("candidate_selectors", [])}
                      if field == "series" and error else set((error or {}).get("candidate_values", [])))
        if len(expected) == 1:
            passed = error is None and actual == next(iter(expected))
        else:
            passed = bool(error and error.get("reason_code") == "ambiguous_" + field
                          and resolved is None and candidates == expected)
        check = {"field": field, "input": token, "brand_scope": scope,
                 "status": "resolved" if error is None else error.get("reason_code"), "passed": passed}
        report["checks"].append(check)
        if not passed:
            report["resolution_failures"].append({**check, "expected": sorted(expected),
                                                  "resolved": resolved, "error": error})

    # All names are checked both with and without brand scope. Expected
    # ambiguity counts as a pass only if every real candidate is retained.
    seen = set()
    for item in identities:
        brand, name = item["brand_name"], item["series"]
        tokens = variants(name) | series_aliases(brand, name)
        for token in sorted(tokens):
            for scope in (None, brand):
                if (token, scope) in seen:
                    continue
                seen.add((token, scope))
                allowed = lambda rows: {x for x in rows if scope is None or x[0] == scope}
                expected = (allowed(by_name.get(token, set()))
                            or allowed(by_normalized.get(key(token), set()))
                            or allowed(by_alias.get(key(token), set())))
                resolved, error = resolve_keyword(snapshot, "series", token,
                                                 {"brands": [scope]} if scope else {}, "audit")
                record("series", token, scope, expected, resolved, error)

    for field in ("brand", "city"):
        values = _values(snapshot, field)
        report["fields"][field] = values
        report["counts"][field] = len(values)
        normalized = defaultdict(set)
        for value in values:
            normalized[key(value)].add(value)
        tokens = set().union(*(variants(value) for value in values))
        if field == "city":
            tokens |= {value[:-1] if value.endswith("市") else value + "市" for value in values}
        for token in sorted(tokens - {""}):
            expected = {token} if token in values else normalized.get(key(token), set())
            if not expected and field == "city":
                city_key = key(token)
                expected = normalized.get(city_key[:-1] if city_key.endswith("市") else city_key + "市", set())
            resolved, error = resolve_keyword(snapshot, field, token, {}, "audit")
            record(field, token, None, expected, resolved, error)

    energy_values = sorted({str(v).strip() for v in snapshot.dataframe["energy_type"].dropna()
                            if str(v).strip()}) if "energy_type" in snapshot.dataframe else []
    report["fields"]["energy"] = energy_values
    report["energy_aliases"] = {}
    for token in sorted(set(energy_values) | set(ENERGY_ALIASES)):
        expected = [v for v in ENERGY_ALIASES.get(token, (token,)) if v in energy_values]
        resolved, error = normalize_energy_filter(token, snapshot)
        passed = resolved == expected and bool(error) == (not bool(expected))
        check = {"field": "energy", "input": token, "resolved": resolved, "passed": passed}
        report["checks"].append(check)
        report["energy_aliases"][token] = resolved
        if not passed:
            report["resolution_failures"].append({**check, "expected": expected, "error": error})
    report["counts"].update({
        "series_identities": len(identities), "series_names": len(by_name),
        "energy": len(energy_values), "generated_series_alias_keys": len(by_alias),
        "alias_collisions": len(report["alias_collisions"]),
        "normalized_name_collisions": len(report["normalized_name_collisions"]),
        "shadowed_aliases": len(report["shadowed_aliases"]),
        "checks": len(report["checks"]), "failures": len(report["resolution_failures"]),
    })
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = audit(get_dataset_snapshot())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["counts"], ensure_ascii=False))
    return 1 if report["resolution_failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
