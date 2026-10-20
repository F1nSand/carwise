import { describe, expect, it } from 'vitest'
import { buildAnalysisView } from './analysisView'
import type { AnalysisResult, AnalysisType } from '../../../types/dashboard'

function result(type: AnalysisType, data: Record<string, unknown>): AnalysisResult {
  return { schema_version: '1.0', analysis_type: type, status: 'success', reason_code: null, dataset_version: 'v1', normalized_filters: {}, filters_applied: [], counts: { dataset_rows: 100, scope_rows: 20, valid_rows: 18, excluded_rows: 2 }, data, limitations: [], methodology: {} }
}

describe('analysis views', () => {
  it('converts price histogram bounds to wan while keeping backend counts', () => {
    const view = buildAnalysisView(result('price_distribution', { n: 18, median_yuan: 80_000, p25_yuan: 50_000, p75_yuan: 120_000, bins: [{ lower_yuan: 10_000, upper_yuan: 100_000, include_upper: false, count: 7, share: .4 }] }))
    expect(view.rows[0]).toMatchObject({ 区间下限_万元: 1, 区间上限_万元: 10, 车源数: 7, 占比百分比: 40 })
    expect(view.summary).toContain('8 万元')
    expect(view.option?.series).toBeTruthy()
    expect(view.option?.series?.[0]).toHaveProperty('markLine')
    expect(view.option?.series?.[0]).toHaveProperty('data.0.itemStyle.color')
    expect(view.option?.series?.[0]).toHaveProperty('markLine.data.0.xAxis', 0)
  })

  it('shows correlation scatter separately from full-sample coefficients', () => {
    const view = buildAnalysisView(result('mileage_price_correlation', { n: 18, pearson_r: .54, spearman_rho: .5, scatter: { total_points: 18, display_points: 2, sampled: true, points: [{ mileage_km: 20_000, price_yuan: 100_000 }] }, mileage_bins: [{ lower_km: 0, upper_km: 10_000, n: 18, median_price_yuan: 80_000, p25_price_yuan: 60_000, p75_price_yuan: 100_000 }] }))
    expect(view.summary).toContain('2 / 18')
    expect(view.option?.series).toBeTruthy()
    expect(view.rows[0].价格中位数_万元).toBe(8)
  })

  it('renders comparison intervals, brand share, and region baseline using their own units', () => {
    const comparison = buildAnalysisView(result('model_comparison', { groups: [{ brand_name: 'A', series: 'X', n: 10, status: 'success', price: { median_yuan: 100_000, p25_yuan: 80_000, p75_yuan: 120_000 }, mileage: { median_km: 50_000 }, age: { median_years: 4 }, support: { low_sample: false } }] }))
    expect(comparison.rows[0].价格中位数_万元).toBe(10)
    expect(comparison.option?.series).toBeTruthy()
    expect(comparison.option?.series?.[0]).toMatchObject({ encode: { x: [1, 2, 3], y: 0 } })
    expect(comparison.option?.yAxis).toMatchObject({ inverse: true })
    expect(comparison.option?.xAxis).toMatchObject({ name: '挂牌价（万元）', nameLocation: 'middle' })
    const brand = buildAnalysisView(result('brand_profile', { denominator_n: 20, groups: [{ brand_name: 'A', n: 10, share: .5, price: { median: 100_000, p25: 80_000, p75: 120_000 }, series_count: 4, mileage: {}, age: {}, category: 'brand' }] }))
    expect(brand.rows[0].当前样本占比百分比).toBe(50)
    expect(brand.rows[0].价格中位数_万元).toBe(10)
    expect(brand.option?.xAxis).toMatchObject({ name: '品牌占比（%）', nameLocation: 'middle' })
    const region = buildAnalysisView(result('region_gap', { baseline_median_yuan: 100_000, baseline_label: 'filtered_sample', valid_city_count: 1, cities: [{ city: '北京', n: 20, median_yuan: 110_000, p25_yuan: 90_000, p75_yuan: 130_000, diff_yuan: 10_000, diff_pct: .1, eligible: true, low_sample: false }] }))
    expect(region.rows[0].相对基准差_万元).toBe(1)
    expect(region.rows[0].相对基准差百分比).toBe(0.1)
    expect(region.summary).toContain('当前筛选样本')
  })

  it('uses recorded snapshot age and preserves missing-age counts', () => {
    const view = buildAnalysisView(result('age_price_relation', { n: 18, missing_age_n: 2, correlation: { pearson_r: -.2, spearman_rho: -.3 }, bins: [{ lower_years: 0, upper_years: 1, n: 8, median_price_yuan: 90_000, p25_price_yuan: 80_000, p75_price_yuan: 100_000 }], scatter: { total_points: 18, display_points: 18, sampled: false, points: [{ age_years: 0.5, price_yuan: 90_000 }] } }))
    expect(view.rows[0].车龄下限_年).toBe(0)
    expect(view.summary).toContain('缺失车龄 2 条')
    expect(view.option?.series).toHaveLength(3)
    expect(view.option?.legend).toMatchObject({ data: [{ name: 'P25–P75 区间', itemStyle: { color: '#83a88c' } }, { name: '挂牌价中位数', itemStyle: { color: '#bb5739' } }] })
  })

  it('keeps every eligible city in the ranked chart and every city in the data table', () => {
    const cities = Array.from({ length: 14 }, (_, index) => ({ city: `城市${index}`, n: 20, median_yuan: 100_000 + index * 1_000, p25_yuan: 80_000 + index * 1_000, p75_yuan: 120_000 + index * 1_000, diff_yuan: index * 1_000, diff_pct: index, eligible: true, low_sample: false }))
    cities.push({ city: '低样本甲', n: 4, median_yuan: 99_000, p25_yuan: 90_000, p75_yuan: 110_000, diff_yuan: -1_000, diff_pct: -1, eligible: false, low_sample: true })
    const view = buildAnalysisView(result('region_gap', { baseline_median_yuan: 100_000, baseline_label: 'filtered_sample', valid_city_count: 14, cities }))
    expect(view.rows).toHaveLength(15)
    expect((view.option?.yAxis as { data: string[] }).data).toHaveLength(14)
    expect(view.intervals).toBeUndefined()
    expect(view.summary).toContain('全部达标城市')
    expect(view.chartHint).toContain('初始显示 12 个城市')
    expect(view.option?.dataZoom).toMatchObject([{ yAxisIndex: 0, startValue: 0, endValue: 11 }, { yAxisIndex: 0, startValue: 0, endValue: 11, orient: 'vertical' }])
    expect(view.option?.grid).toMatchObject({ left: 10, containLabel: true })
    expect(view.option?.xAxis).toMatchObject({ nameLocation: 'middle', name: '相对基准差（万元）' })
  })

  it('escapes backend category names in chart tooltips', () => {
    const view = buildAnalysisView(result('brand_profile', { denominator_n: 4, groups: [{ brand_name: '<b>车型</b>', n: 4, share: 1, price: {}, mileage: {}, age: {} }] }))
    const formatter = view.option?.tooltip && (view.option.tooltip as { formatter: (items: Array<{ dataIndex: number }>) => string }).formatter
    expect(formatter?.([{ dataIndex: 0 }])).toContain('&lt;b&gt;车型&lt;/b&gt;')
    expect(formatter?.([{ dataIndex: 0 }])).not.toContain('<b>车型</b>')
    expect(view.option?.xAxis).toMatchObject({ nameLocation: 'middle', name: '品牌占比（%）' })
    expect(view.option?.grid).toMatchObject({ left: 12, containLabel: true })
  })
})
