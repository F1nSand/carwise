"""Read-only analysis benchmark; vehicle records are never written to SQLite.

Run from the project root: .venv/Scripts/python scripts/benchmark_analysis.py
Use --base-url http://127.0.0.1:8000 to also check HTTP concurrency/admission.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import ctypes
import json
import math
import os
from pathlib import Path
import platform
import statistics
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def resident_bytes() -> int | None:
    if sys.platform == "win32":
        class Counters(ctypes.Structure):
            _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong)] + [
                (name, ctypes.c_size_t) for name in (
                    "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                    "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage",
                    "PagefileUsage", "PeakPagefileUsage",
                )
            ]
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = ctypes.c_void_p
        api = ctypes.WinDLL("psapi", use_last_error=True)
        api.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(Counters), ctypes.c_ulong]
        if api.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            return int(counters.WorkingSetSize)
    elif Path("/proc/self/statm").exists():
        pages = int(Path("/proc/self/statm").read_text().split()[1])
        return pages * os.sysconf("SC_PAGE_SIZE")
    return None


def timing_summary(values: list[float]) -> dict:
    if not values:
        return {"count": 0}
    ordered = sorted(values)
    return {"count": len(values), "median_seconds": statistics.median(values),
            "p95_seconds": ordered[max(0, math.ceil(len(values) * .95) - 1)],
            "max_seconds": max(values)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=20,
                        help="samples per filter scope; at least 20 are required for per-scope p95")
    parser.add_argument("--output", default="artifacts/analysis-validation/benchmark.json")
    parser.add_argument("--base-url")
    parser.add_argument("--min-http-success-rate", type=float, default=0.95)
    parser.add_argument("--http-clients", type=int, default=2,
                        help="normal-load HTTP concurrency; overload probe remains fixed at 4 clients")
    parser.add_argument("--http-samples", type=int, default=20,
                        help="requests in each HTTP run; at least 20 for latency gates")
    args = parser.parse_args()
    if args.iterations < 20:
        parser.error("--iterations must be at least 20 for the planned p95 check")
    if not 0 <= args.min_http_success_rate <= 1:
        parser.error("--min-http-success-rate must be between 0 and 1")
    if args.http_clients < 1:
        parser.error("--http-clients must be positive")
    if args.http_samples < 20:
        parser.error("--http-samples must be at least 20 for the HTTP p95 gate")

    import pandas as pd
    import pyarrow
    from backend.analysis.service import build_catalog, run_dashboard
    from backend import data_loader
    from backend.data_loader import get_dataset_snapshot

    rss_before = resident_bytes()
    started = time.perf_counter()
    snapshot = get_dataset_snapshot()
    cold_load = time.perf_counter() - started
    catalog = build_catalog(snapshot)
    selectors = [item for item in catalog["series_selectors"] if item["brand_name"] == "宝马"][:2]
    cases = [("全量", {}), ("单品牌", {"brands": ["宝马"]}),
             ("多城市与价格", {"cities": ["北京", "上海"], "price_max_yuan": 200000}),
             ("品牌车系", {"series_selectors": selectors})]

    failures: list[str] = []

    def compute(filters: dict) -> tuple[float, float, int, dict]:
        start = time.perf_counter()
        result = run_dashboard(filters, snapshot=snapshot)
        compute_elapsed = time.perf_counter() - start
        serialize_started = time.perf_counter()
        encoded = json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")
        serialize_elapsed = time.perf_counter() - serialize_started
        versions = {value["dataset_version"] for value in result["analyses"].values()}
        if versions != {snapshot.version}:
            failures.append("mixed dataset versions")
        if any(value["status"] == "error" for value in result["analyses"].values()):
            failures.append("analysis returned error status")
        for value in result["analyses"].values():
            scatter = value.get("data", {}).get("scatter", {})
            if scatter.get("display_points", 0) > 2000:
                failures.append("scatter display limit exceeded")
        return compute_elapsed, serialize_elapsed, len(encoded), {
            "scope_rows": result["overview"]["scope_rows"], "status": result["status"]}

    compute(cases[0][1])  # Warm algorithm/import allocations before repeated measurement.
    rss_warm = resident_bytes()
    durations, serialization_durations, sizes, rss, details = [], [], [], [], []
    by_scope: dict[str, list[float]] = {name: [] for name, _ in cases}
    for scope, filters in cases:
        for _ in range(args.iterations):
            elapsed, serialization_elapsed, size, summary = compute(filters)
            durations.append(elapsed)
            serialization_durations.append(serialization_elapsed)
            by_scope[scope].append(elapsed)
            sizes.append(size)
            rss.append(resident_bytes())
            details.append({"scope": scope, "compute_seconds": elapsed,
                            "json_encode_seconds": serialization_elapsed,
                            "json_bytes": size, **summary})
    concurrency_started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=4) as executor:
        concurrent = list(executor.map(compute, [cases[i % len(cases)][1]
                                                  for i in range(args.iterations * len(cases))]))

    scope_summaries = {name: timing_summary(samples) for name, samples in by_scope.items()}
    scope_targets = {f"scope_{name}_p95_le_2_seconds": summary["p95_seconds"] <= 2
                     for name, summary in scope_summaries.items()}

    out = {"environment": {"platform": platform.platform(), "python": platform.python_version(),
                           "pandas": pd.__version__, "pyarrow": pyarrow.__version__,
                           "cpu_count": os.cpu_count(), "processes": 1},
           "dataset_version": snapshot.version, "dataset_rows": len(snapshot.dataframe),
           "sampling": {"iterations_per_filter_scope": args.iterations,
                        "filter_scope_count": len(cases),
                        "serial_compute_samples": args.iterations * len(cases)},
           "cold_snapshot_load_seconds": cold_load,
           "snapshot_cache": {"cached_versions": len(data_loader._snapshot_cache),
                              "estimated_deep_dataframe_bytes": data_loader._snapshot_cache_bytes,
                              "byte_budget": data_loader._MAX_SNAPSHOT_CACHE_BYTES,
                              "over_budget": data_loader._snapshot_cache_bytes > data_loader._MAX_SNAPSHOT_CACHE_BYTES},
           "warm_serial": {"compute": timing_summary(durations),
                           "json_encode": timing_summary(serialization_durations)},
           "by_filter_scope": scope_summaries,
           "json_max_bytes": max(sizes), "serial_samples": details,
           "rss_bytes": {"before_load": rss_before, "after_warmup": rss_warm,
                         "after_serial": rss[-1], "peak_observed": max(x for x in rss if x is not None) if any(x is not None for x in rss) else None,
                         "serial_samples": rss, "after_concurrency": resident_bytes()},
           "direct_compute_concurrency": {"clients": 4, "gate": "not applicable; direct service calls",
                                          "wall_seconds": time.perf_counter() - concurrency_started,
                                          "compute": timing_summary([x[0] for x in concurrent]),
                                          "json_encode": timing_summary([x[1] for x in concurrent])},
           "targets": {**scope_targets,
                       "json_le_2_mb": max(sizes) <= 2_000_000,
                       "analysis_no_errors": not failures,
                       "snapshot_cache_within_budget": data_loader._snapshot_cache_bytes <= data_loader._MAX_SNAPSHOT_CACHE_BYTES}}

    if args.base_url:
        base = args.base_url.rstrip("/")
        def http_compute(index: int) -> dict:
            req = Request(base + "/v1/analysis/dashboard", data=json.dumps({"filters": cases[index % len(cases)][1]}).encode(),
                          headers={"Content-Type": "application/json"}, method="POST")
            start = time.perf_counter()
            try:
                with urlopen(req, timeout=30) as response:
                    body = response.read()
                    return {"status": response.status, "seconds": time.perf_counter() - start,
                            "response_bytes": len(body)}
            except HTTPError as exc:
                exc.read()
                return {"status": exc.code, "retry_after": exc.headers.get("Retry-After"),
                        "seconds": time.perf_counter() - start}
            except (URLError, TimeoutError, OSError) as exc:
                return {"status": 0, "error": str(exc), "seconds": time.perf_counter() - start}
        def run_http_load(clients: int) -> list[dict]:
            with ThreadPoolExecutor(max_workers=clients) as executor:
                return list(executor.map(http_compute, range(args.http_samples)))

        normal_samples = run_http_load(args.http_clients)
        normal_successes = [sample for sample in normal_samples if sample["status"] == 200]
        normal_success_rate = len(normal_successes) / len(normal_samples)
        overload_samples = run_http_load(4)
        overload_503 = [sample for sample in overload_samples if sample["status"] == 503]
        retry_after_503 = [sample for sample in overload_503 if sample.get("retry_after")]
        recovery = http_compute(0)
        normal_success_p95 = timing_summary([sample["seconds"] for sample in normal_successes]).get("p95_seconds")
        status_counts = lambda samples: {
            str(code): sum(item["status"] == code for item in samples)
            for code in sorted({item["status"] for item in samples})
        }
        out["http"] = {
            "normal_load": {"clients": args.http_clients, "samples": normal_samples,
                            "status_counts": status_counts(normal_samples),
                            "success_rate": normal_success_rate,
                            "successful_requests": timing_summary([item["seconds"] for item in normal_successes])},
            "overload_probe": {"clients": 4, "samples": overload_samples,
                               "status_counts": status_counts(overload_samples),
                               "http_503_count": len(overload_503),
                               "http_503_with_retry_after_count": len(retry_after_503)},
            "recovery_probe": recovery,
        }
        out["targets"]["http_normal_success_rate"] = normal_success_rate >= args.min_http_success_rate
        out["targets"]["http_normal_success_p95_le_2_seconds"] = (
            normal_success_p95 is not None and normal_success_p95 <= 2
        )
        out["targets"]["http_overload_returns_retry_after_503"] = bool(retry_after_503)
        out["targets"]["http_recovered_after_overload"] = recovery["status"] == 200
        if not out["targets"]["http_normal_success_rate"]:
            failures.append(f"normal-load HTTP success rate {normal_success_rate:.3f} is below {args.min_http_success_rate:.3f}")
        if not out["targets"]["http_normal_success_p95_le_2_seconds"]:
            failures.append(f"normal-load successful HTTP p95 is {normal_success_p95}, expected <= 2 seconds")
        if not retry_after_503:
            failures.append("4-client overload probe observed no HTTP 503 with Retry-After")
        if recovery["status"] != 200:
            failures.append(f"HTTP recovery probe returned {recovery['status']}")
    out["failures"] = failures
    target = ROOT / args.output
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(out, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps({"output": str(target), "warm_serial": out["warm_serial"],
                      "targets": out["targets"], "failures": failures,
                      "json_max_bytes": out["json_max_bytes"],
                      "http": {key: value.get("status_counts") for key, value in out.get("http", {}).items()
                               if isinstance(value, dict)},
                      "targets": out["targets"]}, ensure_ascii=False))
    if not all(out["targets"].values()):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
