# Shared analysis contract

All metrics are deterministic over one content-addressed `DatasetSnapshot`. Prices are listing prices in yuan; mileage is kilometers; age uses the stored `vehicle_age_years` field. Production year is a separate filter and is never substituted for vehicle age. `dataset_version` is the snapshot SHA-256 identity.

Filters use snake_case: `brands` (max 20), `series_selectors` (max 6 objects containing `brand_name` and `series`), `cities` (max 20), `price_min_yuan` / `price_max_yuan`, `mileage_min_km` / `mileage_max_km`, `year_min` / `year_max`, `age_min_years` / `age_max_years`, and `energy_types`. Lists within one field are OR; fields are AND. Empty lists mean no restriction. Bounds are inclusive. Global rows preserve the existing price and mileage/year anomaly boundaries. Missing values fail an explicitly applied range but otherwise remain in scope and are excluded only from metrics requiring that field.

Every result has `schema_version`, `analysis_type`, `status`, `reason_code`, `dataset_version`, `normalized_filters`, `filters_applied`, `counts` (`dataset_rows`, `scope_rows`, `valid_rows`, `excluded_rows`, `missing_required_rows`, `global_valid_rows`, `global_excluded_rows`, `user_excluded_rows`), `data`, `limitations`, and `methodology`. Global exclusions and user-filter exclusions are counted separately before metric-specific exclusions; their sum plus scope rows equals dataset rows. Status is `success`, `empty`, `unsupported`, or `error`; dashboard status can also be `partial_success`. No matching scope yields `empty` even when the comparison card lacks two selections. Non-finite values serialize as JSON null.

* `price_distribution`: complete valid-price summary, linear P25/P50/P75, and 5–40 equal-width bins (default 20), last bin closed.
* `mileage_price_correlation`: full paired Pearson and average-rank Spearman coefficients (null under n=10 or constant variables), mileage-bin price quantiles, and stable sampled raw-coordinate scatter capped at 2,000 points.
* `model_comparison`: ordered selected brand+series groups with sample count, price quantiles/range, mileage and age medians, and low-sample marker. Fewer than two selections is unsupported for that card only.
* `brand_profile`: top 1–20 brands (default 10), each share using current scope denominator, price quantiles, series count and mileage/age medians; remaining brands and unknown brand are separate conserving groups.
* `region_gap`: city-level n, price median/P25/P75, difference and percent against the current filtered sample median, plus eligibility at min_city_n (default 15). Threshold does not alter baseline.
* `age_price_relation`: stored-age/price pairs and Pearson/Spearman, 1-year bins by default, summaries only for bins with n>=10, missing age count and sampled raw scatter.

`GET /v1/analysis/catalog` reports the exact snapshot version, supported choices, field coverage, ranges, defaults, and unknown collection date range. `POST /v1/analysis/query` takes analysis_type, filters, params, optional expected_dataset_version. `POST /v1/analysis/dashboard` takes filters and optional expected_dataset_version, captures a single snapshot and returns `overview` plus an `analyses` object keyed by all six analysis types. A stale expected version is HTTP 409 (`dataset_changed`); invalid inputs are 422; no matching rows are successful HTTP 200 empty results. Inputs never accept file paths, SQL, or expressions.

The machine-readable representative responses and data member names are in `analysis-api-contract.json`.

## Compatibility baseline

On the local cleaned dataset snapshot `8d4629e5628c294a0dadef13e2a4438ba2d0a72c39b948cefc8671290e6e0ed5`, the legacy `region_price_gap("宝马3系", min_city_n=15)` baseline was 3,086 city-valid rows, national median listing price ¥162,800, and 27 cities meeting the threshold. The shared `region_gap` result with the equivalent brand+series selector returns the same baseline count, median, and eligible-city count. Existing `compare_same_model("宝马3系")` remains an IQR-based tool and returned n=3,089, median ¥162,800, P25 ¥115,800, P75 ¥220,000; this is intentionally a different population/summary from the dashboard's full valid-price distribution.
