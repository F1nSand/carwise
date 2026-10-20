export const ANALYSIS_TYPES = [
  'price_distribution',
  'mileage_price_correlation',
  'model_comparison',
  'brand_profile',
  'region_gap',
  'age_price_relation',
] as const

export type AnalysisType = (typeof ANALYSIS_TYPES)[number]
export type AnalysisStatus = 'success' | 'empty' | 'unsupported' | 'error'
export type DashboardStatus = AnalysisStatus | 'partial_success'

export interface SeriesSelector { brand_name: string; series: string }
export interface DashboardFilters {
  brands: string[]
  series_selectors: SeriesSelector[]
  cities: string[]
  price_min_yuan: number | null
  price_max_yuan: number | null
  mileage_min_km: number | null
  mileage_max_km: number | null
  year_min: number | null
  year_max: number | null
  age_min_years: number | null
  age_max_years: number | null
  energy_types: string[]
}

/** Human-facing editable values; price and mileage are displayed in wan units. */
export interface DashboardDraft {
  brands: string[]
  seriesSelectors: SeriesSelector[]
  cities: string[]
  priceMinWan: number | null
  priceMaxWan: number | null
  mileageMinWanKm: number | null
  mileageMaxWanKm: number | null
  yearMin: number | null
  yearMax: number | null
  ageMinYears: number | null
  ageMaxYears: number | null
  energyTypes: string[]
}

export interface NumericRange { min: number; max: number }
export interface DashboardCatalog {
  schema_version?: string
  dataset_version: string
  brands: string[]
  series_selectors: SeriesSelector[]
  cities: string[]
  energy_types: string[]
  fields: Record<string, { coverage: number; unit: string | null; supported_filter: boolean }>
  ranges: Record<string, NumericRange>
  defaults: { filters: DashboardFilters; params: Record<string, Record<string, number>> }
  data_time_range: { status: string; start: string | null; end: string | null }
}

export interface AnalysisCounts {
  dataset_rows: number
  scope_rows: number
  valid_rows: number
  excluded_rows: number
  missing_required_rows?: number
  [key: string]: number | undefined
}
export interface AnalysisResult<T = Record<string, unknown>> {
  schema_version: string
  analysis_type: AnalysisType
  status: AnalysisStatus
  reason_code: string | null
  dataset_version: string
  normalized_filters: Partial<DashboardFilters>
  filters_applied: string[]
  counts: AnalysisCounts
  data: T
  limitations: string[]
  methodology: Record<string, unknown>
}
export interface DashboardOverview {
  dataset_rows: number
  scope_rows: number
  price_valid_rows: number
  city_valid_rows: number
  vehicle_age_valid_rows: number
  [key: string]: number
}
export interface DashboardResponse {
  schema_version: string
  status: DashboardStatus
  dataset_version: string
  normalized_filters: Partial<DashboardFilters>
  overview: DashboardOverview
  analyses: Record<AnalysisType, AnalysisResult | null>
}

export interface DashboardApiError extends Error { code?: string; status?: number; retryable?: boolean }

export function emptyDashboardFilters(): DashboardFilters {
  return {
    brands: [], series_selectors: [], cities: [], price_min_yuan: null, price_max_yuan: null,
    mileage_min_km: null, mileage_max_km: null, year_min: null, year_max: null,
    age_min_years: null, age_max_years: null, energy_types: [],
  }
}

export function toDashboardDraft(filters: DashboardFilters): DashboardDraft {
  return {
    brands: [...filters.brands], seriesSelectors: filters.series_selectors.map((item) => ({ ...item })),
    cities: [...filters.cities], priceMinWan: fromYuan(filters.price_min_yuan), priceMaxWan: fromYuan(filters.price_max_yuan),
    mileageMinWanKm: fromKm(filters.mileage_min_km), mileageMaxWanKm: fromKm(filters.mileage_max_km),
    yearMin: filters.year_min, yearMax: filters.year_max, ageMinYears: filters.age_min_years, ageMaxYears: filters.age_max_years,
    energyTypes: [...filters.energy_types],
  }
}

export function toDashboardFilters(draft: DashboardDraft): DashboardFilters {
  return {
    brands: [...draft.brands], series_selectors: draft.seriesSelectors.map((item) => ({ ...item })), cities: [...draft.cities],
    price_min_yuan: toYuan(draft.priceMinWan), price_max_yuan: toYuan(draft.priceMaxWan),
    mileage_min_km: toKm(draft.mileageMinWanKm), mileage_max_km: toKm(draft.mileageMaxWanKm),
    year_min: nullableDraftNumber(draft.yearMin), year_max: nullableDraftNumber(draft.yearMax), age_min_years: nullableDraftNumber(draft.ageMinYears), age_max_years: nullableDraftNumber(draft.ageMaxYears),
    energy_types: [...draft.energyTypes],
  }
}

function fromYuan(value: number | null): number | null { return typeof value === 'number' ? value / 10_000 : null }
function fromKm(value: number | null): number | null { return typeof value === 'number' ? value / 10_000 : null }
function nullableDraftNumber(value: number | null): number | null { return value == null || value === ('' as unknown) ? null : typeof value === 'number' ? value : Number.NaN }
function toYuan(value: number | null): number | null { const number = nullableDraftNumber(value); return number === null ? null : number * 10_000 }
function toKm(value: number | null): number | null { const number = nullableDraftNumber(value); return number === null ? null : number * 10_000 }
