import type { AnalysisResult, AnalysisType } from '../../../types/dashboard'

type Row = Record<string, unknown>
export interface AnalysisView {
  rows: Row[]
  columns: Array<{ key: string; label: string }>
  option: Record<string, unknown> | null
  alternateOption?: Record<string, unknown> | null
  primaryLabel?: string
  alternateLabel?: string
  intervals?: Array<{ label: string; p25: number; median: number; p75: number; min: number; max: number }>
  intervalLabel?: string
  chartHint?: string
  summary: string
}

const yuanToWan = (value: unknown): number | null => typeof value === 'number' && Number.isFinite(value) ? value / 10_000 : null
const kmToWan = (value: unknown): number | null => typeof value === 'number' && Number.isFinite(value) ? value / 10_000 : null
const record = (value: unknown): Row => value && typeof value === 'object' && !Array.isArray(value) ? value as Row : {}
const array = (value: unknown): Row[] => Array.isArray(value) ? value.filter((item): item is Row => !!item && typeof item === 'object' && !Array.isArray(item)) : []
const numeric = (value: unknown): number | null => typeof value === 'number' && Number.isFinite(value) ? value : null
const format = (value: unknown, digits = 2): string => typeof value === 'number' && Number.isFinite(value) ? value.toLocaleString('zh-CN', { maximumFractionDigits: digits }) : '—'
const escapeHtml = (value: unknown): string => String(value ?? '').replace(/[&<>"']/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[char]!)
const palette = { forest: '#254d3b', leaf: '#83a88c', vermilion: '#bb5739', blue: '#537a95', gold: '#c39b50', purple: '#8d7998', grid: '#dedfd7', ink: '#202821' }

function baseOption(unit: string, zoom = false): Record<string, unknown> {
  return {
    animationDuration: 220,
    grid: { top: 36, right: 20, bottom: zoom ? 62 : 46, left: 56, containLabel: true },
    tooltip: { trigger: 'axis', confine: true, axisPointer: { type: 'shadow' } },
    xAxis: { type: 'category', axisLabel: { color: palette.ink, hideOverlap: true, interval: 'auto' }, axisLine: { lineStyle: { color: palette.grid } }, axisTick: { alignWithLabel: true } },
    yAxis: { type: 'value', name: unit, nameTextStyle: { color: palette.ink }, axisLabel: { color: palette.ink }, splitLine: { lineStyle: { color: palette.grid } } },
    ...(zoom ? { dataZoom: [{ type: 'inside', filterMode: 'none' }, { type: 'slider', height: 18, bottom: 8 }] } : {}),
  }
}

function intervalOption(title: string, groups: Array<{ label: string; p25: number | null; median: number | null; p75: number | null }>): Record<string, unknown> | null {
  const values = groups.flatMap((group) => [group.p25, group.p75]).filter((value): value is number => value !== null)
  if (!groups.length || !values.length) return null
  const option = baseOption('挂牌价（万元）')
  option.grid = { top: 30, right: 18, bottom: 52, left: 12, containLabel: true }
  option.color = [palette.leaf, palette.vermilion]
  option.xAxis = { type: 'value', name: '挂牌价（万元）', nameLocation: 'middle', nameGap: 32, axisLabel: { color: palette.ink }, splitLine: { lineStyle: { color: palette.grid } } }
  option.yAxis = { type: 'category', inverse: true, data: groups.map((group) => group.label), axisLabel: { color: palette.ink, width: 84, overflow: 'break', lineHeight: 14 } }
  option.tooltip = { trigger: 'item', confine: true, formatter: (params: { dataIndex: number }) => {
    const group = groups[params.dataIndex ?? 0]
    return `${escapeHtml(group.label)}<br>挂牌价 P25–P75：${format(group.p25)}–${format(group.p75)} 万元<br>中位价：${format(group.median)} 万元`
  } }
  option.series = [{
    name: title, type: 'custom', renderItem: (_params: unknown, api: { value: (index: number) => number; coord: (value: number[]) => number[]; size: (value: number[]) => number[] }) => {
      const index = api.value(0); const low = api.value(1); const median = api.value(2); const high = api.value(3)
      if (![index, low, median, high].every(Number.isFinite)) return null
      const lowPoint = api.coord([low, index]); const highPoint = api.coord([high, index]); const medianPoint = api.coord([median, index]); const width = Math.min(24, api.size([0, 1])[1] * 0.34)
      return { type: 'group', children: [
        { type: 'line', shape: { x1: lowPoint[0], y1: lowPoint[1], x2: highPoint[0], y2: highPoint[1] }, style: { stroke: palette.leaf, lineWidth: 5, lineCap: 'round' } },
        { type: 'circle', shape: { cx: medianPoint[0], cy: medianPoint[1], r: 6 }, style: { fill: palette.vermilion, stroke: '#fff', lineWidth: 2 } },
        { type: 'line', shape: { x1: lowPoint[0], y1: lowPoint[1] - width / 2, x2: lowPoint[0], y2: lowPoint[1] + width / 2 }, style: { stroke: palette.forest, lineWidth: 2 } },
        { type: 'line', shape: { x1: highPoint[0], y1: highPoint[1] - width / 2, x2: highPoint[0], y2: highPoint[1] + width / 2 }, style: { stroke: palette.forest, lineWidth: 2 } },
      ] }
    },
    encode: { x: [1, 2, 3], y: 0 },
    data: groups.map((item, index) => [index, item.p25, item.median, item.p75]),
  }]
  return option
}

function createPriceDistribution(data: Row): AnalysisView {
  const bins = array(data.bins)
  const rows = bins.map((bin) => ({
    区间下限_万元: yuanToWan(bin.lower_yuan), 区间上限_万元: yuanToWan(bin.upper_yuan),
    是否包含上限: bin.include_upper, 车源数: bin.count, 占比百分比: typeof bin.share === 'number' ? bin.share * 100 : null,
  }))
  const maxCount = Math.max(1, ...bins.map((bin) => numeric(bin.count) ?? 0))
  const medianIndex = bins.findIndex((bin) => typeof data.median_yuan === 'number' && data.median_yuan >= Number(bin.lower_yuan) && (data.median_yuan < Number(bin.upper_yuan) || (bin.include_upper && data.median_yuan === Number(bin.upper_yuan))))
  const option = bins.length ? {
    ...baseOption('车源数'),
    xAxis: { type: 'category', data: bins.map((bin) => `${format(yuanToWan(bin.lower_yuan))}–${format(yuanToWan(bin.upper_yuan))} 万元`), axisLabel: { color: palette.ink, rotate: bins.length > 12 ? 35 : 0, hideOverlap: true }, axisLine: { lineStyle: { color: palette.grid } } },
    color: [palette.forest],
    legend: { data: ['车源数'], top: 0, textStyle: { color: palette.ink } },
    series: [{ type: 'bar', name: '车源数', data: bins.map((bin) => ({ value: numeric(bin.count), itemStyle: { color: ['#dbe8dd', '#b7d0bd', palette.leaf, '#4d775a', palette.forest][Math.min(4, Math.floor((Number(bin.count) || 0) / maxCount * 5))] } })), itemStyle: { color: palette.forest, borderRadius: [4, 4, 0, 0] }, barMaxWidth: 32, markLine: medianIndex >= 0 ? { symbol: 'none', lineStyle: { color: palette.vermilion, type: 'dashed', width: 2 }, label: { show: false }, data: [{ xAxis: medianIndex }] } : undefined }],
    tooltip: { trigger: 'axis', confine: true, formatter: (items: Array<{ dataIndex: number }>) => { const bin = bins[items[0]?.dataIndex ?? 0]; return `${format(yuanToWan(bin.lower_yuan))}–${format(yuanToWan(bin.upper_yuan))} 万元<br>车源数：${format(bin.count, 0)}<br>占比：${format(typeof bin.share === 'number' ? bin.share * 100 : null)}%` } },
  } : null
  const summary = `挂牌价中位数 ${format(yuanToWan(data.median_yuan))} 万元；P25–P75 为 ${format(yuanToWan(data.p25_yuan))}–${format(yuanToWan(data.p75_yuan))} 万元。`
  return { rows, columns: [{ key: '区间下限_万元', label: '区间下限（万元）' }, { key: '区间上限_万元', label: '区间上限（万元）' }, { key: '是否包含上限', label: '含上限' }, { key: '车源数', label: '车源数' }, { key: '占比百分比', label: '占比（%）' }], option, summary, chartHint: medianIndex >= 0 ? `中位挂牌价 ${format(yuanToWan(data.median_yuan))} 万元；虚线标记中位数所在分箱。` : undefined }
}

function createCorrelation(data: Row, age = false): AnalysisView {
  const scatter = record(data.scatter)
  const points = array(scatter.points)
  const bins = array(age ? data.bins : data.mileage_bins)
  const xField = age ? 'age_years' : 'mileage_km'
  const xLabel = age ? '车龄（年）' : '里程（万公里）'
  const xValue = (point: Row) => age ? numeric(point.age_years) : kmToWan(point.mileage_km)
  const rows = bins.map((bin) => age ? ({
    车龄下限_年: bin.lower_years, 车龄上限_年: bin.upper_years, 样本数: bin.n,
    价格中位数_万元: yuanToWan(bin.median_price_yuan), P25_万元: yuanToWan(bin.p25_price_yuan), P75_万元: yuanToWan(bin.p75_price_yuan),
  }) : ({
    里程下限_万公里: kmToWan(bin.lower_km), 里程上限_万公里: kmToWan(bin.upper_km), 样本数: bin.n,
    价格中位数_万元: yuanToWan(bin.median_price_yuan), P25_万元: yuanToWan(bin.p25_price_yuan), P75_万元: yuanToWan(bin.p75_price_yuan),
  }))
  const xValues = points.map(xValue).filter((value): value is number => value !== null)
  const yValues = points.map((point) => yuanToWan(point.price_yuan)).filter((value): value is number => value !== null)
  const scatterOption = points.length ? {
    animationDuration: 180,
    grid: { top: 30, right: 18, bottom: 58, left: 64, containLabel: true },
    tooltip: { trigger: 'item', confine: true, formatter: (params: { value: number[] }) => `${xLabel.split('（')[0]}：${format(params.value[0])} ${age ? '年' : '万公里'}<br>挂牌价：${format(params.value[1])} 万元` },
    xAxis: { type: 'value', name: xLabel, nameLocation: 'middle', nameGap: 34, axisLabel: { color: palette.ink }, splitLine: { lineStyle: { color: palette.grid } } },
    yAxis: { type: 'value', name: '挂牌价（万元）', axisLabel: { color: palette.ink }, splitLine: { lineStyle: { color: palette.grid } } },
    series: [{ type: 'scatter', name: '展示样本', data: points.flatMap((point) => { const x = xValue(point); const y = yuanToWan(point.price_yuan); return x === null || y === null ? [] : [[x, y]] }), symbolSize: 6, large: true, largeThreshold: 1000, progressive: 500, itemStyle: { color: palette.forest, opacity: .3 } }],
  } : null
  const categories = bins.map((bin) => age ? `${format(bin.lower_years)}–${format(bin.upper_years)} 年` : `${format(kmToWan(bin.lower_km))}–${format(kmToWan(bin.upper_km))} 万公里`)
  const binOption = bins.some((bin) => yuanToWan(bin.median_price_yuan) !== null) ? {
    ...baseOption('挂牌价（万元）'), grid: { top: 42, right: 14, bottom: 62, left: 12, containLabel: true },
    color: [palette.leaf, palette.vermilion],
    legend: { data: [{ name: 'P25–P75 区间', itemStyle: { color: palette.leaf } }, { name: '挂牌价中位数', itemStyle: { color: palette.vermilion } }], top: 0, textStyle: { color: palette.ink } },
    tooltip: { trigger: 'axis', confine: true, formatter: (items: Array<{ dataIndex: number }>) => { const bin = bins[items[0]?.dataIndex ?? 0]; const low = yuanToWan(bin.p25_price_yuan); const high = yuanToWan(bin.p75_price_yuan); const median = yuanToWan(bin.median_price_yuan); return `${escapeHtml(categories[items[0]?.dataIndex ?? 0])}<br>有效样本：${format(bin.n, 0)}<br>P25–P75：${format(low)}–${format(high)} 万元<br>中位挂牌价：${format(median)} 万元` } },
    xAxis: { type: 'category', data: categories, axisLabel: { color: palette.ink, rotate: bins.length > 12 ? 35 : 0, hideOverlap: true } },
    series: [
      { type: 'line', name: 'P25 基线', stack: 'quartile-band', data: bins.map((bin) => yuanToWan(bin.p25_price_yuan)), symbol: 'none', lineStyle: { color: 'transparent', opacity: 0 }, itemStyle: { color: 'transparent', opacity: 0 }, areaStyle: { color: 'transparent', opacity: 0 }, silent: true },
      { type: 'line', name: 'P25–P75 区间', stack: 'quartile-band', data: bins.map((bin) => { const low = yuanToWan(bin.p25_price_yuan); const high = yuanToWan(bin.p75_price_yuan); return low === null || high === null ? null : high - low }), symbol: 'none', lineStyle: { color: palette.leaf, opacity: 0 }, itemStyle: { color: palette.leaf, opacity: 0 }, areaStyle: { color: palette.leaf, opacity: .2 }, connectNulls: false },
      { type: 'line', name: '挂牌价中位数', data: bins.map((bin) => yuanToWan(bin.median_price_yuan)), symbolSize: 6, lineStyle: { color: age ? palette.vermilion : palette.forest, width: 2.5 }, itemStyle: { color: age ? palette.vermilion : palette.forest }, connectNulls: false },
    ],
  } : null
  const correlation = record(age ? data.correlation : data)
  const scatterNote = `${format(scatter.display_points, 0)} / ${format(scatter.total_points, 0)} 个配对点用于展示${scatter.sampled ? '（稳定抽样）' : ''}。`
  const correlationReason = age ? correlation.reason : data.correlation_reason
  const reasonLabels: Record<string, string> = { insufficient_samples: '配对样本少于 10 条', constant_variable: '至少一个字段没有变化', field_unavailable: '所需字段不可用' }
  const reason = typeof correlationReason === 'string' ? ` 系数未计算原因：${reasonLabels[correlationReason] ?? '原因暂不可用'}。` : ''
  const missing = age ? ` 缺失车龄 ${format(data.missing_age_n, 0)} 条。` : ''
  return { rows, columns: age ? [{ key: '车龄下限_年', label: '车龄下限（年）' }, { key: '车龄上限_年', label: '车龄上限（年）' }, { key: '样本数', label: '样本数' }, { key: '价格中位数_万元', label: '价格中位数（万元）' }, { key: 'P25_万元', label: 'P25（万元）' }, { key: 'P75_万元', label: 'P75（万元）' }] : [{ key: '里程下限_万公里', label: '里程下限（万公里）' }, { key: '里程上限_万公里', label: '里程上限（万公里）' }, { key: '样本数', label: '样本数' }, { key: '价格中位数_万元', label: '价格中位数（万元）' }, { key: 'P25_万元', label: 'P25（万元）' }, { key: 'P75_万元', label: 'P75（万元）' }], option: age ? (binOption ?? scatterOption) : (scatterOption ?? binOption), alternateOption: age ? scatterOption : binOption, primaryLabel: age ? '显示年龄分箱' : '显示散点', alternateLabel: age ? '切换到散点图' : '切换到里程分箱摘要', summary: `${xLabel.split('（')[0]}与挂牌价相关性：Pearson r=${format(correlation.pearson_r)}，Spearman ρ=${format(correlation.spearman_rho)}。${scatterNote}${missing}${reason}` }
}

function createComparison(data: Row): AnalysisView {
  const groups = array(data.groups)
  const rows = groups.map((group) => {
    const price = record(group.price); const mileage = record(group.mileage); const age = record(group.age); const support = record(group.support)
    return { 品牌: group.brand_name, 车系: group.series, 状态: group.status, 样本数: group.n, 价格中位数_万元: yuanToWan(price.median_yuan), P25_万元: yuanToWan(price.p25_yuan), P75_万元: yuanToWan(price.p75_yuan), 里程中位数_万公里: kmToWan(mileage.median_km), 车龄中位数_年: age.median_years, 低样本: support.low_sample }
  })
  const option = intervalOption('价格区间', groups.map((group) => { const price = record(group.price); return { label: `${String(group.brand_name ?? '')} ${String(group.series ?? '')}`.trim() + (record(group.support).low_sample ? ' · 低样本' : ''), p25: yuanToWan(price.p25_yuan), median: yuanToWan(price.median_yuan), p75: yuanToWan(price.p75_yuan) } }))
  return { rows, columns: Object.keys(rows[0] ?? {}).map((key) => ({ key, label: key.replaceAll('_万元', '（万元）').replaceAll('_万公里', '（万公里）').replaceAll('_年', '（年）') })), option, summary: '挂牌价以 P25–P75 区间表示，圆点为中位数；图表标签中的“低样本”表示样本少于 10 条。里程和车龄在独立列中呈现。' }
}

function createBrandProfile(data: Row): AnalysisView {
  const groups = array(data.groups)
  const rows = groups.map((group) => {
    const price = record(group.price); const mileage = record(group.mileage); const age = record(group.age)
    return { 品牌: group.brand_name, 类别: group.category, 车源数: group.n, 当前样本占比百分比: typeof group.share === 'number' ? group.share * 100 : null, 价格中位数_万元: yuanToWan(price.median), P25_万元: yuanToWan(price.p25), P75_万元: yuanToWan(price.p75), 车系数: group.series_count, 里程中位数_万公里: kmToWan(mileage.median_km), 车龄中位数_年: age.median_years }
  })
  const rankedGroups = [...groups].sort((left, right) => (numeric(right.share) ?? 0) - (numeric(left.share) ?? 0))
  const option = rankedGroups.length ? {
    ...baseOption('占比（%）'), grid: { top: 30, right: 16, bottom: 62, left: 12, containLabel: true },
    tooltip: { trigger: 'axis', confine: true, formatter: (items: Array<{ dataIndex: number }>) => { const group = rankedGroups[items[0]?.dataIndex ?? 0]; return `${escapeHtml(group.brand_name ?? group.category ?? '未知')}<br>当前样本占比：${format(numeric(group.share) === null ? null : Number(group.share) * 100)}%<br>车源数：${format(group.n, 0)}` } },
    color: [palette.forest],
    xAxis: { type: 'value', name: '品牌占比（%）', nameLocation: 'middle', nameGap: 32, axisLabel: { color: palette.ink }, splitLine: { lineStyle: { color: palette.grid } } },
    yAxis: { type: 'category', inverse: true, data: rankedGroups.map((group) => String(group.brand_name ?? group.category ?? '未知')), axisLabel: { color: palette.ink, width: 78, overflow: 'break', lineHeight: 14 } },
    series: [{ type: 'bar', name: '当前样本占比', data: rankedGroups.map((group) => typeof group.share === 'number' ? group.share * 100 : 0), itemStyle: { color: palette.forest, borderRadius: [0, 4, 4, 0] }, barMaxWidth: 24 }],
  } : null
  const intervals = groups.flatMap((group) => {
    const price = record(group.price); const p25 = yuanToWan(price.p25); const median = yuanToWan(price.median); const p75 = yuanToWan(price.p75)
    return p25 !== null && median !== null && p75 !== null ? [{ label: String(group.brand_name ?? group.category ?? '未知'), p25, median, p75, min: p25, max: p75 }] : []
  })
  const allValues = intervals.flatMap((item) => [item.p25, item.p75]); const min = Math.min(...allValues); const max = Math.max(...allValues)
  return { rows, columns: Object.keys(rows[0] ?? {}).map((key) => ({ key, label: key.replaceAll('_万元', '（万元）').replaceAll('_万公里', '（万公里）').replaceAll('_年', '（年）').replaceAll('_百分比', '（%）') })), option, intervals: intervals.map((item) => ({ ...item, min, max })), intervalLabel: '各品牌挂牌价 P25–P75 区间（万元），圆点为中位数', summary: `当前筛选内共 ${format(data.denominator_n, 0)} 条车源；品牌占比以这个分母计算，不是全国份额。` }
}

function createRegion(data: Row): AnalysisView {
  const cities = array(data.cities)
  const rows = cities.map((city) => ({ 城市: city.city, 样本数: city.n, 价格中位数_万元: yuanToWan(city.median_yuan), P25_万元: yuanToWan(city.p25_yuan), P75_万元: yuanToWan(city.p75_yuan), 相对基准差_万元: yuanToWan(city.diff_yuan), 相对基准差百分比: typeof city.diff_pct === 'number' ? city.diff_pct : null, 可参与比较: city.eligible, 低样本: city.low_sample }))
  const eligibleCities = cities.filter((city) => city.eligible)
  const chartCities = [...eligibleCities].sort((left, right) => Math.abs(numeric(right.diff_yuan) ?? 0) - Math.abs(numeric(left.diff_yuan) ?? 0))
  const option = chartCities.length ? {
    ...baseOption('相对挂牌价基准（万元）'), grid: { top: 30, right: 30, bottom: 68, left: 10, containLabel: true },
    tooltip: { trigger: 'axis', confine: true, formatter: (items: Array<{ dataIndex: number }>) => { const city = chartCities[items[0]?.dataIndex ?? 0]; return `${escapeHtml(city.city ?? '未知')}<br>挂牌价中位数差：${format(yuanToWan(city.diff_yuan))} 万元<br>城市中位挂牌价：${format(yuanToWan(city.median_yuan))} 万元<br>有效样本：${format(city.n, 0)}${city.low_sample ? '<br>低样本' : ''}` } },
    xAxis: { type: 'value', name: '相对基准差（万元）', nameLocation: 'middle', nameGap: 36, axisLabel: { color: palette.ink }, splitLine: { lineStyle: { color: palette.grid } }, axisLine: { onZero: true, lineStyle: { color: palette.ink } } },
    yAxis: { type: 'category', inverse: true, data: chartCities.map((city) => String(city.city ?? '未知')), axisLabel: { color: palette.ink, width: 76, overflow: 'break', lineHeight: 14 } },
    dataZoom: chartCities.length > 12 ? [
      { type: 'inside', yAxisIndex: 0, startValue: 0, endValue: 11, filterMode: 'none', zoomOnMouseWheel: false, moveOnMouseWheel: true },
      { type: 'slider', yAxisIndex: 0, orient: 'vertical', width: 12, right: 4, top: 38, bottom: 62, startValue: 0, endValue: 11, filterMode: 'none', showDetail: false, brushSelect: false },
    ] : undefined,
    series: [{ type: 'bar', name: '中位价差', data: chartCities.map((city) => ({ value: yuanToWan(city.diff_yuan), itemStyle: { color: (numeric(city.diff_yuan) ?? 0) < 0 ? palette.purple : palette.blue } })), barMaxWidth: 22, markLine: { symbol: 'none', lineStyle: { color: palette.ink, width: 1 }, label: { formatter: '基准 0 万元', color: palette.ink }, data: [{ xAxis: 0 }] } }],
  } : null
  const baseline = yuanToWan(data.baseline_median_yuan)
  const baselineLabel = data.baseline_label === 'selected_city_sample' ? '所选城市样本' : '当前筛选样本'
  return { rows, columns: Object.keys(rows[0] ?? {}).map((key) => ({ key, label: key.replaceAll('_万元', '（万元）').replaceAll('_百分比', '（%）') })), option, chartHint: chartCities.length > 12 ? `图表初始显示 12 个城市；使用图内滚轮或右侧滑块浏览其余 ${chartCities.length - 12} 个，数据表保留全部城市。` : undefined, summary: `${cities.length === 1 ? '仅有一个城市，显示其统计值，不比较城市优势。' : ''}基准：${baselineLabel}，中位挂牌价 ${format(baseline)} 万元；共 ${format(data.valid_city_count, 0)} 个有效城市。图表列出全部达标城市，低样本与不达标城市保留在完整数据表中。` }
}

export function isObject(value: unknown): value is Row { return !!value && typeof value === 'object' && !Array.isArray(value) }

export function buildAnalysisView(result: AnalysisResult): AnalysisView {
  const data = record(result.data)
  const builders: Record<AnalysisType, () => AnalysisView> = {
    price_distribution: () => createPriceDistribution(data),
    mileage_price_correlation: () => createCorrelation(data),
    model_comparison: () => createComparison(data),
    brand_profile: () => createBrandProfile(data),
    region_gap: () => createRegion(data),
    age_price_relation: () => createCorrelation(data, true),
  }
  return builders[result.analysis_type]()
}
