import {
  ANALYSIS_TYPES,
  type AnalysisResult,
  type AnalysisType,
  type DashboardCatalog,
  type DashboardFilters,
  type DashboardResponse,
} from '../types/dashboard'

const apiBaseUrl = String(import.meta.env.VITE_API_BASE_URL || '').replace(/\/$/, '')

export class DashboardRequestError extends Error {
  constructor(message: string, readonly code: string, readonly status?: number, readonly retryable = false) {
    super(message)
    this.name = 'DashboardRequestError'
  }
}

async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response
  try {
    response = await fetch(`${apiBaseUrl}${path}`, { ...init, credentials: 'include' })
  } catch (error) {
    if (init?.signal?.aborted) throw error
    throw new DashboardRequestError('无法连接分析服务，请检查网络后重试。', 'network_error', undefined, true)
  }
  if (!response.ok) {
    let detail: Record<string, unknown> = {}
    try {
      const body = await response.json() as { detail?: unknown; error?: unknown }
      const value = body.detail && typeof body.detail === 'object' ? body.detail as Record<string, unknown> : {}
      detail = value
      if (!Object.keys(detail).length && typeof body.detail === 'string') detail.message = body.detail
      if (!Object.keys(detail).length && typeof body.error === 'string') detail.message = body.error
    } catch { /* the HTTP status remains useful when there is no JSON error body */ }
    const code = String(detail.code || detail.reason_code || (response.status === 409 ? 'dataset_changed' : 'request_failed'))
    const message = code === 'dataset_changed'
      ? '数据版本已更新，请刷新目录后重新应用筛选。'
      : code === 'analysis_busy'
        ? '分析服务当前繁忙，请稍后重试。'
        : String(detail.message || detail.detail || `请求失败（HTTP ${response.status}）`)
    throw new DashboardRequestError(message, code, response.status, response.status >= 500 || response.status === 429)
  }
  let value: unknown
  try { value = await response.json() } catch { throw new DashboardRequestError('分析服务返回了无法读取的数据。', 'invalid_response', response.status) }
  return value as T
}

function isRecord(value: unknown): value is Record<string, unknown> { return !!value && typeof value === 'object' && !Array.isArray(value) }

export function validateCatalog(value: unknown): DashboardCatalog {
  if (!isRecord(value) || typeof value.dataset_version !== 'string' || !value.dataset_version ||
      !Array.isArray(value.brands) || !Array.isArray(value.series_selectors) || !Array.isArray(value.cities) ||
      !Array.isArray(value.energy_types) || !isRecord(value.fields) || !isRecord(value.ranges) || !isRecord(value.defaults)) {
    throw new DashboardRequestError('分析服务目录格式不完整，请稍后重试。', 'invalid_response')
  }
  return value as unknown as DashboardCatalog
}

export function validateDashboard(value: unknown): DashboardResponse {
  if (!isRecord(value) || typeof value.dataset_version !== 'string' || !['success', 'empty', 'error', 'unsupported', 'partial_success'].includes(String(value.status)) || !isRecord(value.overview) || !isRecord(value.analyses)) {
    throw new DashboardRequestError('分析服务返回了不完整的看板数据。', 'invalid_response')
  }
  for (const type of ANALYSIS_TYPES) {
    const analysis = value.analyses[type]
    if (analysis !== null && (!isRecord(analysis) || analysis.analysis_type !== type || !['success', 'empty', 'unsupported', 'error'].includes(String(analysis.status)) || !isRecord(analysis.counts) || !isRecord(analysis.data) || !Array.isArray(analysis.limitations) || !Array.isArray(analysis.filters_applied))) {
      throw new DashboardRequestError(`分析服务返回的${type}结果格式不完整。`, 'invalid_response')
    }
  }
  return value as unknown as DashboardResponse
}

export async function getDashboardCatalog(signal?: AbortSignal): Promise<DashboardCatalog> {
  return validateCatalog(await requestJson<unknown>('/v1/analysis/catalog', { signal }))
}

export async function getDashboard(filters: DashboardFilters, datasetVersion: string, signal?: AbortSignal): Promise<DashboardResponse> {
  return validateDashboard(await requestJson<unknown>('/v1/analysis/dashboard', {
    method: 'POST', signal, headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ filters, expected_dataset_version: datasetVersion }),
  }))
}

export async function getAnalysis(type: AnalysisType, filters: DashboardFilters, datasetVersion: string, signal?: AbortSignal): Promise<AnalysisResult> {
  const result = await requestJson<unknown>('/v1/analysis/query', {
    method: 'POST', signal, headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ analysis_type: type, filters, params: {}, expected_dataset_version: datasetVersion }),
  })
  if (!isRecord(result) || result.analysis_type !== type || !['success', 'empty', 'unsupported', 'error'].includes(String(result.status)) || result.dataset_version !== datasetVersion || !isRecord(result.counts) || !isRecord(result.data) || !Array.isArray(result.limitations) || !Array.isArray(result.filters_applied)) {
    throw new DashboardRequestError(`分析服务返回的${type}结果格式不完整。`, 'invalid_response')
  }
  return result as unknown as AnalysisResult
}

export function isAnalysisType(value: string): value is AnalysisType { return (ANALYSIS_TYPES as readonly string[]).includes(value) }

export function serializeFilters(filters: DashboardFilters): string { return JSON.stringify(filters) }

export function parseFilters(raw: unknown, catalog: DashboardCatalog): DashboardFilters | null {
  if (typeof raw !== 'string') return null
  try {
    const value = JSON.parse(raw) as Record<string, unknown>
    if (!isRecord(value)) return null
    const arrays = [value.brands, value.series_selectors, value.cities, value.energy_types]
    if (arrays.some((item) => item !== undefined && !Array.isArray(item))) return null
    const brands = Array.isArray(value.brands) ? value.brands : []
    const series = Array.isArray(value.series_selectors) ? value.series_selectors : []
    const cities = Array.isArray(value.cities) ? value.cities : []
    const energy = Array.isArray(value.energy_types) ? value.energy_types : []
    if (brands.some((item) => typeof item !== 'string') || cities.some((item) => typeof item !== 'string') || energy.some((item) => typeof item !== 'string') || series.some((item) => !isRecord(item) || typeof item.brand_name !== 'string' || typeof item.series !== 'string')) return null
    const filters = {
      brands: brands as string[],
      series_selectors: series as Array<{ brand_name: string; series: string }>,
      cities: cities as string[],
      price_min_yuan: nullableNumber(value.price_min_yuan), price_max_yuan: nullableNumber(value.price_max_yuan),
      mileage_min_km: nullableNumber(value.mileage_min_km), mileage_max_km: nullableNumber(value.mileage_max_km),
      year_min: nullableNumber(value.year_min), year_max: nullableNumber(value.year_max),
      age_min_years: nullableNumber(value.age_min_years), age_max_years: nullableNumber(value.age_max_years),
      energy_types: energy as string[],
    }
    if (filters.brands.some((item) => !catalog.brands.includes(item)) || filters.cities.some((item) => !catalog.cities.includes(item)) ||
        filters.energy_types.some((item) => !catalog.energy_types.includes(item)) || filters.series_selectors.some((item) => !catalog.series_selectors.some((entry) => entry.brand_name === item.brand_name && entry.series === item.series))) return null
    return filters
  } catch { return null }
}

function nullableNumber(value: unknown): number | null { return value == null ? null : typeof value === 'number' ? value : Number.NaN }
