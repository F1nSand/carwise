import { afterEach, describe, expect, it, vi } from 'vitest'
import { getAnalysis, getDashboard, getDashboardCatalog, parseFilters, serializeFilters, validateCatalog, validateDashboard } from './dashboardAdapter'
import { emptyDashboardFilters, toDashboardDraft, toDashboardFilters, type DashboardCatalog } from '../types/dashboard'

const catalog: DashboardCatalog = {
  dataset_version: 'sha256:cars-v1', brands: ['比亚迪'], series_selectors: [{ brand_name: '比亚迪', series: '秦PLUS' }], cities: ['北京'], energy_types: ['纯电动'],
  fields: { energy_type: { coverage: 0.1, unit: null, supported_filter: true } }, ranges: {}, defaults: { filters: emptyDashboardFilters(), params: {} }, data_time_range: { status: 'unknown', start: null, end: null },
}
const makeAnalysis = (type: string) => ({ schema_version: '1.0', analysis_type: type, status: 'empty', reason_code: 'no_matching_rows', dataset_version: catalog.dataset_version, normalized_filters: {}, filters_applied: [], counts: { dataset_rows: 3, scope_rows: 0, valid_rows: 0, excluded_rows: 0 }, data: {}, limitations: [], methodology: {} })
const dashboard = { schema_version: '1.0', status: 'success', dataset_version: catalog.dataset_version, normalized_filters: {}, overview: { dataset_rows: 3, scope_rows: 3, price_valid_rows: 3, city_valid_rows: 3, vehicle_age_valid_rows: 3 }, analyses: Object.fromEntries(['price_distribution', 'mileage_price_correlation', 'model_comparison', 'brand_profile', 'region_gap', 'age_price_relation'].map((type) => [type, makeAnalysis(type)])) }

afterEach(() => { vi.unstubAllGlobals() })

describe('dashboard adapter', () => {
  it('converts display units once on the API boundary', () => {
    const filters = emptyDashboardFilters()
    filters.price_min_yuan = 158_000
    filters.mileage_max_km = 88_000
    const draft = toDashboardDraft(filters)
    expect(draft.priceMinWan).toBe(15.8)
    expect(draft.mileageMaxWanKm).toBe(8.8)
    expect(toDashboardFilters(draft)).toEqual(filters)
    draft.priceMinWan = '' as unknown as number
    expect(toDashboardFilters(draft).price_min_yuan).toBeNull()
  })

  it('round trips URL filters and rejects stale catalog choices', () => {
    const filters = { ...emptyDashboardFilters(), brands: ['比亚迪'], series_selectors: catalog.series_selectors, cities: ['北京'] }
    expect(parseFilters(serializeFilters(filters), catalog)).toEqual(filters)
    expect(parseFilters(serializeFilters({ ...filters, brands: ['旧品牌'] }), catalog)).toBeNull()
  })

  it('validates catalog and each required dashboard result envelope', () => {
    expect(validateCatalog(catalog)).toEqual(catalog)
    expect(() => validateCatalog({ brands: [] })).toThrow(/目录格式不完整/)
    expect(validateDashboard(dashboard).analyses.price_distribution?.analysis_type).toBe('price_distribution')
    const partial = { ...dashboard, status: 'partial_success', analyses: { ...dashboard.analyses, region_gap: { ...makeAnalysis('region_gap'), status: 'error', reason_code: 'analysis_failed' } } }
    expect(validateDashboard(partial).analyses.price_distribution?.status).toBe('empty')
    expect(validateDashboard(partial).analyses.region_gap?.status).toBe('error')
    expect(() => validateDashboard({ ...dashboard, analyses: { ...dashboard.analyses, region_gap: {} } })).toThrow(/region_gap/)
  })

  it('uses credentials and sends expected snapshot version with filters', async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(new Response(JSON.stringify(catalog), { status: 200 })).mockResolvedValueOnce(new Response(JSON.stringify(dashboard), { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)
    await getDashboardCatalog()
    await getDashboard(emptyDashboardFilters(), catalog.dataset_version)
    expect(fetchMock.mock.calls[0][1]).toMatchObject({ credentials: 'include' })
    const init = fetchMock.mock.calls[1][1] as RequestInit
    expect(init.credentials).toBe('include')
    expect(JSON.parse(String(init.body))).toMatchObject({ expected_dataset_version: catalog.dataset_version, filters: emptyDashboardFilters() })
  })

  it('accepts a valid query result only for the requested type and data version', async () => {
    const validResult = { ...makeAnalysis('price_distribution'), status: 'success', filters_applied: ['品牌：比亚迪'] }
    const fetchMock = vi.fn().mockResolvedValueOnce(new Response(JSON.stringify(validResult), { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)
    expect(await getAnalysis('price_distribution', emptyDashboardFilters(), catalog.dataset_version)).toMatchObject({ status: 'success', dataset_version: catalog.dataset_version })
    const wrongVersion = vi.fn().mockResolvedValueOnce(new Response(JSON.stringify({ ...validResult, dataset_version: 'stale' }), { status: 200 }))
    vi.stubGlobal('fetch', wrongVersion)
    await expect(getAnalysis('price_distribution', emptyDashboardFilters(), catalog.dataset_version)).rejects.toThrow(/格式不完整/)
    const missingFilters = vi.fn().mockResolvedValueOnce(new Response(JSON.stringify({ ...validResult, filters_applied: undefined }), { status: 200 }))
    vi.stubGlobal('fetch', missingFilters)
    await expect(getAnalysis('price_distribution', emptyDashboardFilters(), catalog.dataset_version)).rejects.toThrow(/格式不完整/)
  })
})
