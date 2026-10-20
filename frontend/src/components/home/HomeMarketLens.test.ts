import { mount, flushPromises } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import HomeMarketLens from './HomeMarketLens.vue'
import type { DataCapabilities } from '../../services/analysisAdapter'

const getAnalysis = vi.hoisted(() => vi.fn())
vi.mock('../../services/dashboardAdapter', () => ({ getAnalysis }))

const capabilities: DataCapabilities = { dataset_version: 'snapshot-1', row_count: 12, fields: {}, energy_values: [], supported_filters: [], unsupported_filters: [], data_time_range: { status: 'unknown', start: null, end: null } }
function response(overrides: Record<string, unknown> = {}) {
  return {
    schema_version: '1.0', analysis_type: 'price_distribution', status: 'success', reason_code: null,
    dataset_version: 'snapshot-1', normalized_filters: {}, filters_applied: [],
    counts: { dataset_rows: 14, scope_rows: 12, valid_rows: 10, excluded_rows: 2 },
    data: { n: 10, median_yuan: 140_000, bins: [
      { lower_yuan: 100_000, upper_yuan: 150_000, count: 6, share: .6, include_upper: false },
      { lower_yuan: 150_000, upper_yuan: 200_000, count: 4, share: .4, include_upper: true },
    ] }, limitations: [], methodology: {}, ...overrides,
  }
}
const deferred = <T,>() => {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => { resolve = done })
  return { promise, resolve }
}

describe('HomeMarketLens', () => {
  beforeEach(() => { getAnalysis.mockReset(); getAnalysis.mockResolvedValue(response()) })

  it('renders actual histogram bins, median, and known count meanings', async () => {
    const wrapper = mount(HomeMarketLens, { props: { capabilities, capabilitiesLoaded: true } })
    await flushPromises()
    expect(getAnalysis).toHaveBeenCalledWith('price_distribution', expect.objectContaining({ price_min_yuan: 100_000, price_max_yuan: 200_000 }), 'snapshot-1', expect.any(AbortSignal))
    expect(wrapper.text()).toContain('14 万元')
    expect(wrapper.text()).toContain('10 条有效挂牌价')
    expect(wrapper.text()).toContain('范围内 12 条')
    expect(wrapper.findAll('.market-lens__bar')).toHaveLength(2)
    expect(wrapper.get('.market-lens__bar').attributes('y')).toBe('20')
    expect(wrapper.get('svg').attributes('aria-label')).toContain('10 条有效挂牌价')
    expect(wrapper.text()).toContain('查看分箱数据')
    expect(wrapper.get('.market-lens__budgets button:nth-child(3)').attributes('aria-pressed')).toBe('true')
    await wrapper.get('.market-lens__budgets button:first-child').trigger('click')
    await flushPromises()
    expect(getAnalysis.mock.calls.at(-1)?.[1]).toMatchObject({ price_min_yuan: null, price_max_yuan: null })
    expect(wrapper.text()).toContain('高价长尾会拉宽价格区间')
  })

  it('preserves the server median precision in ten-thousand-yuan units', async () => {
    getAnalysis.mockResolvedValueOnce(response({ data: { n: 10, median_yuan: 119_800, bins: [{ lower_yuan: 0, upper_yuan: 200_000, count: 10, share: 1 }] } }))
    const wrapper = mount(HomeMarketLens, { props: { capabilities, capabilitiesLoaded: true } })
    await flushPromises()
    expect(wrapper.text()).toContain('11.98 万元')
  })

  it('sends exact yuan boundaries for selected budget ranges and emits a draft query', async () => {
    const wrapper = mount(HomeMarketLens, { props: { capabilities, capabilitiesLoaded: true } })
    await flushPromises()
    await wrapper.get('.market-lens__budgets button:nth-child(2)').trigger('click')
    await flushPromises()
    expect(getAnalysis.mock.calls.at(-1)?.[1]).toMatchObject({ price_min_yuan: null, price_max_yuan: 100_000 })
    await wrapper.get('.market-lens__budgets button:nth-child(3)').trigger('click')
    await flushPromises()
    expect(getAnalysis.mock.calls.at(-1)?.[1]).toMatchObject({ price_min_yuan: 100_000, price_max_yuan: 200_000 })
    await wrapper.get('.market-lens__action').trigger('click')
    expect(wrapper.emitted('select')?.[0]?.[0]).toContain('10 万至 20 万元')
  })

  it('shows empty and retryable error states without retaining a chart', async () => {
    getAnalysis.mockResolvedValueOnce(response({ status: 'empty', counts: { dataset_rows: 12, scope_rows: 0, valid_rows: 0, excluded_rows: 0 }, data: { n: 0, median_yuan: null, bins: [] } }))
    const wrapper = mount(HomeMarketLens, { props: { capabilities, capabilitiesLoaded: true } })
    await flushPromises()
    expect(wrapper.text()).toContain('没有可展示的挂牌价样本')
    expect(wrapper.find('.market-lens__chart').exists()).toBe(false)
    getAnalysis.mockRejectedValueOnce(new Error('服务暂不可用'))
    await wrapper.get('.market-lens__budgets button:nth-child(2)').trigger('click')
    await flushPromises()
    expect(wrapper.get('[role="alert"]').text()).toContain('服务暂不可用')
    getAnalysis.mockResolvedValueOnce(response())
    await wrapper.get('[role="alert"] button').trigger('click')
    await flushPromises()
    expect(wrapper.find('.market-lens__chart').exists()).toBe(true)
  })

  it('shows an API error status as an error even when its valid count is zero', async () => {
    getAnalysis.mockResolvedValueOnce(response({ status: 'error', counts: { dataset_rows: 12, scope_rows: 0, valid_rows: 0, excluded_rows: 0 }, data: { n: 0, median_yuan: null, bins: [] } }))
    const wrapper = mount(HomeMarketLens, { props: { capabilities, capabilitiesLoaded: true } })
    await flushPromises()
    expect(wrapper.get('[role="alert"]').text()).toContain('无法生成价格分布')
    expect(wrapper.text()).not.toContain('没有可展示的挂牌价样本')
    expect(wrapper.find('.market-lens__chart').exists()).toBe(false)
  })

  it('aborts the old budget request and ignores its late response', async () => {
    const first = deferred<ReturnType<typeof response>>()
    getAnalysis.mockReturnValueOnce(first.promise)
    const wrapper = mount(HomeMarketLens, { props: { capabilities, capabilitiesLoaded: true } })
    const firstSignal = getAnalysis.mock.calls[0][3] as AbortSignal
    await wrapper.get('.market-lens__budgets button:nth-child(2)').trigger('click')
    expect(firstSignal.aborted).toBe(true)
    await flushPromises()
    first.resolve(response({ data: { n: 99, median_yuan: 9_900_000, bins: [{ lower_yuan: 0, upper_yuan: 1, count: 99, share: 1 }] } }))
    await flushPromises()
    expect(wrapper.text()).toContain('14 万元')
    expect(wrapper.findAll('.market-lens__bar')).toHaveLength(2)
    wrapper.unmount()
  })

  it('waits for capabilities and re-queries when snapshot version changes', async () => {
    const wrapper = mount(HomeMarketLens, { props: { capabilities: null, capabilitiesLoaded: false } })
    expect(getAnalysis).not.toHaveBeenCalled()
    await wrapper.setProps({ capabilities, capabilitiesLoaded: true })
    await flushPromises()
    await wrapper.setProps({ capabilities: { ...capabilities, dataset_version: 'snapshot-2' } })
    await flushPromises()
    expect(getAnalysis).toHaveBeenCalledTimes(2)
    expect(getAnalysis.mock.calls[1][2]).toBe('snapshot-2')
  })
})
