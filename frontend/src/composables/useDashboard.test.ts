import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import { defineComponent, onMounted } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { validateDashboardFilters } from './useDashboard'
import { useDashboard } from './useDashboard'
import { emptyDashboardFilters, type DashboardCatalog } from '../types/dashboard'

const catalog: DashboardCatalog = {
  dataset_version: 'v1', brands: ['A', 'B'], series_selectors: [{ brand_name: 'A', series: 'same' }, { brand_name: 'B', series: 'same' }], cities: ['北京'], energy_types: ['纯电'],
  fields: {}, ranges: {}, defaults: { filters: emptyDashboardFilters(), params: {} }, data_time_range: { status: 'unknown', start: null, end: null },
}
afterEach(() => vi.unstubAllGlobals())

describe('dashboard filter validation', () => {
  it('does not rewrite navigation when a catalog resolves while the dashboard is leaving', async () => {
    let finishCatalog!: (response: Response) => void
    const fetchMock = vi.fn(() => new Promise<Response>((resolve) => { finishCatalog = resolve }))
    vi.stubGlobal('fetch', fetchMock)
    const harness = defineComponent({ setup() { const state = useDashboard(); onMounted(state.initialize); return state }, template: '<div />' })
    const router = createRouter({ history: createMemoryHistory(), routes: [
      { path: '/dashboard', component: harness }, { path: '/chat', component: { template: '<div />' } },
    ] })
    await router.push('/dashboard')
    const wrapper = mount(harness, { global: { plugins: [router] } })
    await flushPromises()
    const replace = vi.spyOn(router, 'replace')
    // A page leaving through a transition remains mounted briefly.
    await router.push('/chat')
    finishCatalog(new Response(JSON.stringify(catalog)))
    await flushPromises()
    expect(replace).not.toHaveBeenCalled()
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(router.currentRoute.value.fullPath).toBe('/chat')
    wrapper.unmount()
  })
  it('rejects invalid numbers, reversed ranges, and unsupported identities', () => {
    expect(validateDashboardFilters({ ...emptyDashboardFilters(), price_min_yuan: 200, price_max_yuan: 100 }, catalog)).toMatch(/下限不能大于上限/)
    expect(validateDashboardFilters({ ...emptyDashboardFilters(), age_min_years: Number.NaN }, catalog)).toMatch(/有效的非负数值/)
    expect(validateDashboardFilters({ ...emptyDashboardFilters(), brands: ['A'], series_selectors: [{ brand_name: 'B', series: 'same' }] }, catalog)).toMatch(/车系必须与当前品牌/)
    expect(validateDashboardFilters({ ...emptyDashboardFilters(), energy_types: ['油电混合'] }, catalog)).toMatch(/能源类型不受/)
  })

  it('allows the same series name when paired with its selected brand identity', () => {
    expect(validateDashboardFilters({ ...emptyDashboardFilters(), brands: ['B'], series_selectors: [{ brand_name: 'B', series: 'same' }] }, catalog)).toBeNull()
  })

  it('aborts the previous request and ignores its late response', async () => {
    const names = ['price_distribution', 'mileage_price_correlation', 'model_comparison', 'brand_profile', 'region_gap', 'age_price_relation']
    const analysisResponse = (scopeRows: number) => ({ schema_version: '1.0', status: 'success', dataset_version: 'v1', normalized_filters: {}, overview: { dataset_rows: 10, scope_rows: scopeRows, price_valid_rows: scopeRows, city_valid_rows: scopeRows, vehicle_age_valid_rows: scopeRows }, analyses: Object.fromEntries(names.map((type) => [type, { schema_version: '1.0', analysis_type: type, status: 'empty', reason_code: 'no_matching_rows', dataset_version: 'v1', normalized_filters: {}, filters_applied: [], counts: { dataset_rows: 10, scope_rows: scopeRows, valid_rows: 0, excluded_rows: scopeRows }, data: {}, limitations: [], methodology: {} }])) })
    let finishOld: ((response: Response) => void) | undefined
    let oldSignal: AbortSignal | undefined
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      if (url.endsWith('/catalog')) return Promise.resolve(new Response(JSON.stringify(catalog)))
      if (!finishOld) {
        oldSignal = init?.signal ?? undefined
        return new Promise<Response>((resolve) => { finishOld = resolve })
      }
      return Promise.resolve(new Response(JSON.stringify(analysisResponse(1))))
    })
    vi.stubGlobal('fetch', fetchMock)
    const harness = defineComponent({ setup() { const state = useDashboard(); onMounted(state.initialize); return { ...state } }, template: '<button type="button" @click="apply">apply</button>' })
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/dashboard', component: harness }] })
    await router.push('/dashboard')
    await router.isReady()
    const wrapper = mount(harness, { global: { plugins: [router] } })
    await flushPromises()
    expect(fetchMock).toHaveBeenCalledTimes(2)
    wrapper.vm.draft.brands = ['A']
    await wrapper.vm.apply()
    await flushPromises()
    expect(wrapper.vm.response.overview.scope_rows).toBe(1)
    expect(oldSignal?.aborted).toBe(true)
    finishOld?.(new Response(JSON.stringify(analysisResponse(9))))
    await flushPromises()
    expect(wrapper.vm.response.overview.scope_rows).toBe(1)
    wrapper.unmount()
  })

  it('keeps a retried card on the dashboard snapshot after the catalog refreshes', async () => {
    const names = ['price_distribution', 'mileage_price_correlation', 'model_comparison', 'brand_profile', 'region_gap', 'age_price_relation']
    const analysisResponse = (scopeRows: number, version = 'v1') => ({ schema_version: '1.0', status: 'success', dataset_version: version, normalized_filters: {}, overview: { dataset_rows: 10, scope_rows: scopeRows, price_valid_rows: scopeRows, city_valid_rows: scopeRows, vehicle_age_valid_rows: scopeRows }, analyses: Object.fromEntries(names.map((type) => [type, { schema_version: '1.0', analysis_type: type, status: 'empty', reason_code: 'no_matching_rows', dataset_version: version, normalized_filters: {}, filters_applied: [], counts: { dataset_rows: 10, scope_rows: scopeRows, valid_rows: 0, excluded_rows: scopeRows }, data: {}, limitations: [], methodology: {} }])) })
    let catalogCalls = 0
    let retryVersion = ''
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      if (url.endsWith('/catalog')) {
        catalogCalls += 1
        return new Response(JSON.stringify(catalogCalls === 1 ? catalog : { ...catalog, dataset_version: 'v2' }))
      }
      if (url.endsWith('/dashboard')) return new Response(JSON.stringify(analysisResponse(10)))
      const body = JSON.parse(String(init?.body)) as { expected_dataset_version: string }
      retryVersion = body.expected_dataset_version
      return new Response(JSON.stringify(analysisResponse(10).analyses.price_distribution))
    })
    vi.stubGlobal('fetch', fetchMock)
    const harness = defineComponent({ setup() { const state = useDashboard(); onMounted(state.initialize); return { ...state } }, template: '<button type="button" @click="retry(\'price_distribution\')">retry</button>' })
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/dashboard', component: harness }] })
    await router.push('/dashboard')
    await router.isReady()
    const wrapper = mount(harness, { global: { plugins: [router] } })
    await flushPromises()
    await wrapper.vm.loadCatalog()
    expect(wrapper.vm.catalog.dataset_version).toBe('v2')

    await wrapper.vm.retry('price_distribution')
    expect(retryVersion).toBe('v1')
    expect(wrapper.vm.response.dataset_version).toBe('v1')
    expect(Object.values(wrapper.vm.response.analyses).every((item) => item?.dataset_version === 'v1')).toBe(true)
    wrapper.unmount()
  })
})
