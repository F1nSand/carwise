import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import AnalysisChart from './AnalysisChart.vue'
import type { AnalysisResult } from '../../types/dashboard'

const echarts = vi.hoisted(() => ({ use: vi.fn(), init: vi.fn(), setOption: vi.fn(), resize: vi.fn(), dispose: vi.fn(), markLine: {} }))
vi.mock('echarts/core', () => ({ use: echarts.use, init: echarts.init }))
vi.mock('echarts/charts', () => ({ BarChart: {}, LineChart: {}, ScatterChart: {}, CustomChart: {} }))
vi.mock('echarts/components', () => ({ GridComponent: {}, TooltipComponent: {}, LegendComponent: {}, DataZoomComponent: {}, MarkLineComponent: echarts.markLine }))
vi.mock('echarts/renderers', () => ({ CanvasRenderer: {} }))

function result(median: number): AnalysisResult {
  return {
    schema_version: '1.0', analysis_type: 'price_distribution', status: 'success', reason_code: null,
    dataset_version: 'v1', normalized_filters: {}, filters_applied: [],
    counts: { dataset_rows: 10, scope_rows: 10, valid_rows: 10, excluded_rows: 0 },
    data: { n: 10, median_yuan: median, p25_yuan: 50_000, p75_yuan: 120_000, bins: [{ lower_yuan: 10_000, upper_yuan: 100_000, include_upper: false, count: 7, share: .7 }] },
    limitations: [], methodology: {},
  }
}

function correlationResult(): AnalysisResult {
  return {
    schema_version: '1.0', analysis_type: 'mileage_price_correlation', status: 'success', reason_code: null,
    dataset_version: 'v1', normalized_filters: {}, filters_applied: [],
    counts: { dataset_rows: 18, scope_rows: 18, valid_rows: 18, excluded_rows: 0 },
    data: { n: 18, pearson_r: .54, spearman_rho: .5,
      scatter: { total_points: 18, display_points: 2, sampled: true, points: [{ mileage_km: 20_000, price_yuan: 100_000 }] },
      mileage_bins: Array.from({ length: 13 }, (_, index) => ({ lower_km: index * 10_000, upper_km: (index + 1) * 10_000, n: 1, median_price_yuan: 80_000 + index, p25_price_yuan: 60_000, p75_price_yuan: 100_000 })) },
    limitations: [], methodology: {},
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  echarts.init.mockReturnValue({ setOption: echarts.setOption, resize: echarts.resize, dispose: echarts.dispose })
})

describe('AnalysisChart ECharts lifecycle', () => {
  it('reuses its container instance for result updates and disposes only on unmount', async () => {
    const wrapper = mount(AnalysisChart, { props: { type: 'price_distribution', title: '挂牌价分布', subtitle: '当前数据', result: result(80_000) } })
    await flushPromises()
    expect(echarts.init).toHaveBeenCalledTimes(1)
    expect(echarts.setOption).toHaveBeenCalledTimes(1)
    expect(echarts.use.mock.calls[0]?.[0]).toContain(echarts.markLine)

    await wrapper.setProps({ result: result(95_000) })
    await flushPromises()
    expect(echarts.init).toHaveBeenCalledTimes(1)
    expect(echarts.setOption).toHaveBeenCalledTimes(2)
    expect(echarts.dispose).not.toHaveBeenCalled()

    wrapper.unmount()
    expect(echarts.dispose).toHaveBeenCalledTimes(1)
  })

  it('replaces the scatter and binned-line series when switching views on the reused instance', async () => {
    const wrapper = mount(AnalysisChart, { props: { type: 'mileage_price_correlation', title: '里程与价格', subtitle: '当前数据', result: correlationResult() } })
    await flushPromises()
    const toggle = wrapper.get('button.table-toggle')
    const firstOption = echarts.setOption.mock.calls[0]?.[0] as { series: Array<{ type: string }> }
    expect(firstOption.series).toHaveLength(1)
    expect(firstOption.series[0]?.type).toBe('scatter')

    await toggle.trigger('click')
    await flushPromises()
    const lineOption = echarts.setOption.mock.calls[1]?.[0] as { series: Array<{ type: string }>; dataZoom?: unknown[] }
    expect(lineOption.series).toHaveLength(3)
    expect(lineOption.series[0]?.type).toBe('line')
    expect(lineOption.series[2]?.name).toBe('挂牌价中位数')
    expect(lineOption.dataZoom).toBeUndefined()

    await wrapper.get('button.table-toggle').trigger('click')
    await flushPromises()
    const finalOption = echarts.setOption.mock.calls[2]?.[0] as { series: Array<{ type: string }>; dataZoom?: unknown[] }
    expect(finalOption.series).toHaveLength(1)
    expect(finalOption.series[0]?.type).toBe('scatter')
    expect(finalOption.dataZoom).toBeUndefined()
    expect(echarts.setOption.mock.calls.map((call) => call[1])).toEqual([{ notMerge: true }, { notMerge: true }, { notMerge: true }])
    expect(echarts.init).toHaveBeenCalledTimes(1)
    wrapper.unmount()
  })
})
