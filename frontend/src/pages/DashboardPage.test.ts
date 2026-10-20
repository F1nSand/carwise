import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import { afterEach, describe, expect, it, vi } from 'vitest'
import DashboardPage from './DashboardPage.vue'
import { emptyDashboardFilters } from '../types/dashboard'

const catalog = {
  dataset_version: 'v1', brands: ['品牌A'], series_selectors: [{ brand_name: '品牌A', series: '车系一' }], cities: ['北京'], energy_types: ['纯电动'],
  fields: { energy_type: { coverage: .085, unit: null, supported_filter: true } }, ranges: { production_year: { min: 1990, max: 2024 } },
  defaults: { filters: emptyDashboardFilters(), params: {} }, data_time_range: { status: 'unknown', start: null, end: null },
}
const types = ['price_distribution', 'mileage_price_correlation', 'model_comparison', 'brand_profile', 'region_gap', 'age_price_relation']
const analysis = (type: string) => ({ schema_version: '1.0', analysis_type: type, status: 'empty', reason_code: 'no_matching_rows', dataset_version: 'v1', normalized_filters: {}, filters_applied: [], counts: { dataset_rows: 0, scope_rows: 0, valid_rows: 0, excluded_rows: 0 }, data: {}, limitations: [], methodology: {} })
const dashboard = { schema_version: '1.0', status: 'success', dataset_version: 'v1', normalized_filters: {}, overview: { dataset_rows: 12, scope_rows: 12, price_valid_rows: 11, city_valid_rows: 12, vehicle_age_valid_rows: 10 }, analyses: Object.fromEntries(types.map((type) => [type, analysis(type)])) }
const nativeAnimate = HTMLElement.prototype.animate

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  if (nativeAnimate) Object.defineProperty(HTMLElement.prototype, 'animate', { configurable: true, writable: true, value: nativeAnimate })
  else Reflect.deleteProperty(HTMLElement.prototype, 'animate')
})

describe('DashboardPage', () => {
  it('hides empty and unsupported analyses and offers one clear-filter recovery prompt', async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(new Response(JSON.stringify(catalog))).mockResolvedValueOnce(new Response(JSON.stringify(dashboard)))
    vi.stubGlobal('fetch', fetchMock)
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/dashboard', component: DashboardPage }] })
    await router.push('/dashboard')
    await router.isReady()
    const wrapper = mount(DashboardPage, { global: { plugins: [router] } })
    await flushPromises()
    expect(fetchMock).toHaveBeenCalledTimes(2)
    expect(wrapper.findAll('.analysis-chart')).toHaveLength(0)
    expect(wrapper.findAll('.empty-results')).toHaveLength(1)
    expect(wrapper.text()).toContain('清空筛选并重试')
    expect(wrapper.text()).not.toContain('挂牌价分布')
    wrapper.unmount()
  })

  it('renders only analyses with drawable data and keeps the filter draft when collapsed', async () => {
    const histogram = { ...analysis('price_distribution'), status: 'success', counts: { dataset_rows: 12, scope_rows: 12, valid_rows: 11, excluded_rows: 1 }, data: { n: 11, median_yuan: 80000, p25_yuan: 50000, p75_yuan: 120000, bins: [{ lower_yuan: 0, upper_yuan: 100000, include_upper: false, count: 7, share: .64 }] } }
    const response = { ...dashboard, analyses: { ...dashboard.analyses, price_distribution: histogram, model_comparison: { ...analysis('model_comparison'), status: 'unsupported' } } }
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(new Response(JSON.stringify(catalog))).mockResolvedValueOnce(new Response(JSON.stringify(response))))
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/dashboard', component: DashboardPage }] })
    await router.push('/dashboard')
    await router.isReady()
    const wrapper = mount(DashboardPage, { global: { plugins: [router] } })
    await flushPromises()
    expect(wrapper.findAll('.analysis-chart')).toHaveLength(1)
    expect(wrapper.text()).toContain('挂牌价分布')
    expect(wrapper.text()).not.toContain('车系横向对比')
    const controls = wrapper.get('#dashboard-filter-controls')
    expect(wrapper.get('.filter-toggle').attributes('aria-controls')).toBe('dashboard-filter-controls')
    expect(wrapper.get('.filter-toggle').attributes('aria-expanded')).toBe('true')
    await wrapper.get('.filter-toggle').trigger('click')
    expect(wrapper.get('.filter-toggle').attributes('aria-expanded')).toBe('false')
    expect(controls.attributes('style')).toContain('display: none')
    wrapper.unmount()
  })

  it('shows a recoverable message when the live API is unavailable', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('offline')))
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/dashboard', component: DashboardPage }] })
    await router.push('/dashboard')
    await router.isReady()
    const wrapper = mount(DashboardPage, { global: { plugins: [router] } })
    await flushPromises()
    expect(wrapper.text()).toContain('分析目录暂时不可用')
    expect(wrapper.text()).toContain('无法连接分析服务')
    wrapper.unmount()
  })

  it('starts with mobile filters collapsed and keeps error analyses visible with their reason and retry action', async () => {
    const errorResult = { ...analysis('price_distribution'), status: 'error', reason_code: 'computation_failed' }
    const response = { ...dashboard, analyses: { ...dashboard.analyses, price_distribution: errorResult } }
    const mediaListeners: Array<(event: MediaQueryListEvent) => void> = []
    vi.stubGlobal('matchMedia', vi.fn((query: string) => ({ matches: query.includes('max-width: 900px'), addEventListener: (_type: string, listener: (event: MediaQueryListEvent) => void) => mediaListeners.push(listener), removeEventListener: vi.fn() })))
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(new Response(JSON.stringify(catalog))).mockResolvedValueOnce(new Response(JSON.stringify(response))))
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/dashboard', component: DashboardPage }] })
    await router.push('/dashboard')
    await router.isReady()
    const wrapper = mount(DashboardPage, { global: { plugins: [router] } })
    await flushPromises()
    expect(wrapper.get('.filter-toggle').attributes('aria-expanded')).toBe('false')
    expect(wrapper.get('#dashboard-filter-controls').attributes('style')).toContain('display: none')
    expect(wrapper.findAll('.analysis-chart')).toHaveLength(1)
    expect(wrapper.text()).toContain('原因代码：computation_failed')
    expect(wrapper.findAll('.panel-state--error .retry')).toHaveLength(1)
    wrapper.unmount()
  })

  it('cancels an active filter FLIP when reduced motion becomes preferred', async () => {
    const reducedMotionListeners: Array<(event: MediaQueryListEvent) => void> = []
    const reducedMotionQuery = { matches: false, addEventListener: (_type: string, listener: (event: MediaQueryListEvent) => void) => reducedMotionListeners.push(listener), removeEventListener: vi.fn() }
    vi.stubGlobal('matchMedia', vi.fn((query: string) => query.includes('max-width') ? { matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() } : reducedMotionQuery))
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(new Response(JSON.stringify(catalog))).mockResolvedValueOnce(new Response(JSON.stringify(dashboard))))
    const animation = { cancel: vi.fn(), onfinish: null, oncancel: null } as unknown as Animation
    Object.defineProperty(HTMLElement.prototype, 'animate', { configurable: true, value: vi.fn(() => animation) })
    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
      const collapsed = !!this.closest('.dashboard-layout')?.classList.contains('dashboard-layout--collapsed')
      const main = this.classList.contains('dashboard-main')
      const left = main ? (collapsed ? 76 : 300) : 0
      const width = main ? (collapsed ? 1000 : 800) : (collapsed ? 56 : 280)
      return { x: left, y: 0, left, top: 0, right: left + width, bottom: 500, width, height: 500, toJSON: () => ({}) } as DOMRect
    })
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/dashboard', component: DashboardPage }] })
    await router.push('/dashboard')
    await router.isReady()
    const wrapper = mount(DashboardPage, { global: { plugins: [router] } })
    await flushPromises()
    await wrapper.get('.filter-toggle').trigger('click')
    await flushPromises()
    expect(HTMLElement.prototype.animate).toHaveBeenCalled()
    reducedMotionListeners.forEach((listener) => listener({ matches: true } as MediaQueryListEvent))
    expect(animation.cancel).toHaveBeenCalled()
    wrapper.unmount()
  })
})
