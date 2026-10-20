<template>
  <section class="analysis-chart" :aria-labelledby="headingId">
    <div class="analysis-chart__heading">
      <div><h3 :id="headingId">{{ title }}</h3><p>{{ subtitle }}</p></div>
      <div class="chart-actions">
        <button v-if="view.alternateOption" type="button" class="table-toggle" @click="showAlternate = !showAlternate"><ScatterChartIcon :size="15" aria-hidden="true" />{{ showAlternate ? view.primaryLabel : view.alternateLabel }}</button>
        <button v-if="result" type="button" class="table-toggle" :aria-expanded="showTable" @click="showTable = !showTable"><Table2 :size="15" aria-hidden="true" />{{ showTable ? '收起数据表' : '查看数据表' }}</button>
      </div>
    </div>

    <div v-if="loading" class="panel-state" role="status"><span class="loader"></span>正在计算这项分析</div>
    <div v-else-if="error" class="panel-state panel-state--error" role="alert"><p>{{ error }}</p><button class="retry" type="button" @click="emit('retry')"><RefreshCw :size="14" />重试此项</button></div>
    <div v-else-if="error || !result || result.status === 'empty' || result.status === 'unsupported' || result.status === 'error'" class="panel-state" :class="{ 'panel-state--error': !!error }" :role="error ? 'alert' : 'status'">
      <p>{{ error || (result?.reason_code ? statusText(result.reason_code) : '尚无结果。应用筛选后将显示分析。') }}</p>
      <button v-if="error || result?.status === 'error' || (result?.status === 'unsupported' && !isSelectionPrompt(result.reason_code))" class="retry" type="button" @click="emit('retry')"><RefreshCw :size="14" />重试此项</button>
    </div>
    <template v-else>
      <div class="chart-meta"><span>指标有效样本 {{ number(result.counts.valid_rows) }}</span><span>当前筛选样本 {{ number(result.counts.scope_rows) }}</span><span>排除 {{ number(result.counts.excluded_rows) }}</span></div>
      <p v-if="result.data && isObject(result.data) && typeof result.data.n === 'number' && result.data.n !== result.counts.valid_rows" class="chart-meta">本项统计 n={{ number(result.data.n) }}</p>
      <p v-if="view.chartHint" class="chart-hint">{{ view.chartHint }}</p>
      <div v-if="!showTable && activeOption" ref="chartElement" class="chart-canvas" role="img" :aria-label="`${title}图表；${summary}`"></div>
      <div v-else-if="!showTable" class="chart-empty" role="status">当前有效数据不足以构成图表，可查看完整数据表。</div>
      <AnalysisDataTable v-if="showTable" :caption="`${title}：后端完整统计数据`" :columns="view.columns" :rows="view.rows" />
      <section v-if="!showTable && view.intervals?.length" class="interval-view" :aria-label="view.intervalLabel">
        <p>{{ view.intervalLabel }}</p>
        <div v-for="item in view.intervals" :key="item.label" class="interval-row">
          <span class="interval-label" :title="item.label">{{ item.label }}</span>
          <div class="interval-track" role="img" :aria-label="`${item.label} P25 ${formatNumber(item.p25)} 万元，中位数 ${formatNumber(item.median)} 万元，P75 ${formatNumber(item.p75)} 万元`"><i :style="intervalStyle(item)" /><b :style="medianStyle(item)" /></div>
          <span class="interval-value">{{ formatNumber(item.p25) }}–{{ formatNumber(item.p75) }}</span>
        </div>
      </section>
      <ul v-if="result.limitations.length" class="limitations"><li v-for="item in result.limitations" :key="item">{{ item }}</li></ul>
      <details v-if="summary" class="method-details"><summary>方法与统计说明</summary><p>{{ summary }}</p></details>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { RefreshCw, ScatterChart as ScatterChartIcon, Table2 } from 'lucide-vue-next'
import type { ECharts, EChartsOption } from 'echarts'
import AnalysisDataTable from './AnalysisDataTable.vue'
import type { AnalysisResult, AnalysisType } from '../../types/dashboard'
import { buildAnalysisView } from './charts/analysisView'

const props = defineProps<{ type: AnalysisType; title: string; subtitle: string; result: AnalysisResult | null; loading?: boolean; error?: string }>()
const emit = defineEmits<{ retry: [] }>()
const headingId = `analysis-${props.type}-${globalThis.crypto?.randomUUID?.() ?? Math.random().toString(36).slice(2, 10)}`
const chartElement = ref<HTMLDivElement | null>(null)
const showTable = ref(false)
const showAlternate = ref(false)
const reducedMotion = ref(false)
const view = computed(() => props.result ? buildAnalysisView(props.result) : { rows: [], columns: [], option: null, summary: '' })
const activeOption = computed(() => showAlternate.value ? view.value.alternateOption : view.value.option)
const summary = computed(() => props.result ? view.value.summary : '')
let chart: ECharts | null = null
let chartTarget: HTMLDivElement | null = null
let observer: ResizeObserver | null = null
let renderSeq = 0
let motionQuery: MediaQueryList | null = null

function number(value: unknown): string { return typeof value === 'number' ? new Intl.NumberFormat('zh-CN').format(value) : '—' }
function formatNumber(value: number): string { return Number.isFinite(value) ? new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 2 }).format(value) : '—' }
function intervalStyle(item: { p25: number; p75: number; min: number; max: number }) { const span = Math.max(item.max - item.min, 1e-9); return { left: `${Math.max(0, (item.p25 - item.min) / span * 100)}%`, width: `${Math.max(1, (item.p75 - item.p25) / span * 100)}%` } }
function medianStyle(item: { median: number; min: number; max: number }) { const span = Math.max(item.max - item.min, 1e-9); return { left: `${Math.min(100, Math.max(0, (item.median - item.min) / span * 100))}%` } }
function statusText(value: string): string {
  const labels: Record<string, string> = { insufficient_samples: '有效样本不足，当前无法给出稳定统计。', unsupported_field: '数据字段暂不支持这项分析。', field_unavailable: '所需数据字段当前不可用。', price_field_unavailable: '挂牌价字段当前不可用。', city_field_unavailable: '城市字段当前不可用。', brand_field_unavailable: '品牌字段当前不可用。', no_matching_rows: '当前筛选没有匹配车源。', constant_variable: '字段没有变化，无法计算相关系数。', insufficient_series: '选择至少两个车系后才能比较；其他分析不受影响。', select_at_least_two_series: '选择至少两个车系后才能比较；其他分析不受影响。' }
  return labels[value] ?? '这项分析暂时没有可展示结果。'
}
function isSelectionPrompt(value: string | null) { return value === 'select_at_least_two_series' || value === 'insufficient_series' }
function correlationReason(value: unknown): string { const labels: Record<string, string> = { insufficient_samples: '配对样本少于 10 条', constant_variable: '至少一个字段没有变化', field_unavailable: '所需字段不可用' }; return typeof value === 'string' ? labels[value] ?? '相关系数未计算' : '' }
function isObject(value: unknown): value is Record<string, unknown> { return !!value && typeof value === 'object' && !Array.isArray(value) }
function dispose() { observer?.disconnect(); observer = null; chart?.dispose(); chart = null; chartTarget = null }

async function render() {
  const sequence = ++renderSeq
  if (!props.result || props.result.status !== 'success' || !activeOption.value || !chartElement.value || showTable.value) { dispose(); return }
  const [{ use, init }, { BarChart, LineChart, ScatterChart, CustomChart }, { GridComponent, TooltipComponent, LegendComponent, DataZoomComponent, MarkLineComponent }, { CanvasRenderer }] = await Promise.all([
    import('echarts/core'), import('echarts/charts'), import('echarts/components'), import('echarts/renderers'),
  ])
  if (sequence !== renderSeq || !chartElement.value) return
  use([BarChart, LineChart, ScatterChart, CustomChart, GridComponent, TooltipComponent, LegendComponent, DataZoomComponent, MarkLineComponent, CanvasRenderer])
  if (!chart || chartTarget !== chartElement.value) {
    dispose()
    chart = init(chartElement.value, undefined, { renderer: 'canvas' })
    chartTarget = chartElement.value
    observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(() => chart?.resize())
    observer?.observe(chartTarget)
  }
  const option = activeOption.value as EChartsOption
  chart.setOption({ ...option, animation: !reducedMotion.value, animationDuration: reducedMotion.value ? 0 : (option.animationDuration ?? 220), animationDurationUpdate: reducedMotion.value ? 0 : (option.animationDurationUpdate ?? 220) }, { notMerge: true })
  chart.resize()
}

watch(() => [props.result, props.loading, showTable.value, showAlternate.value, reducedMotion.value], async () => { await nextTick(); void render() }, { deep: true })
function onMotionPreferenceChange(event: MediaQueryListEvent) { reducedMotion.value = event.matches }
onMounted(() => {
  if (typeof window.matchMedia === 'function') {
    motionQuery = window.matchMedia('(prefers-reduced-motion: reduce)')
    reducedMotion.value = motionQuery.matches
    motionQuery.addEventListener?.('change', onMotionPreferenceChange)
  }
  void render()
})
onBeforeUnmount(() => { motionQuery?.removeEventListener?.('change', onMotionPreferenceChange); motionQuery = null; renderSeq += 1; dispose() })
</script>

<style scoped>
.analysis-chart { display: grid; gap: 12px; min-width: 0; padding: 18px; border: 1px solid var(--color-border); border-radius: 14px; background: var(--color-surface); }
.analysis-chart__heading { display: flex; justify-content: space-between; align-items: flex-start; gap: 14px; }
.chart-actions { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 7px; }
h3 { margin: 0; color: var(--color-navy); font-size: 16px; letter-spacing: -.02em; }
.analysis-chart__heading p { margin: 5px 0 0; color: var(--color-ink-muted); font-size: 12px; line-height: 1.5; }
.table-toggle, .retry { display: inline-flex; min-height: 40px; align-items: center; justify-content: center; gap: 7px; padding: 0 11px; color: var(--color-teal); border: 1px solid var(--color-border-strong); border-radius: 9px; background: var(--color-surface); font-size: 12px; font-weight: 700; white-space: nowrap; cursor: pointer; }
.table-toggle:hover, .retry:hover { border-color: var(--color-teal); background: var(--color-teal-soft); }
.chart-meta { display: flex; flex-wrap: wrap; gap: 8px 18px; margin: 0; color: var(--color-ink-muted); font-size: 12px; }
.chart-hint { margin: -3px 0 0; color: var(--color-ink-muted); font-size: 11px; line-height: 1.5; }
.chart-canvas { width: 100%; height: 270px; min-width: 0; }
.chart-empty { display: grid; min-height: 120px; place-items: center; color: var(--color-ink-muted); border: 1px dashed var(--color-border); border-radius: 10px; font-size: 12px; text-align: center; }
.interval-view { display: grid; gap: 8px; padding-top: 3px; }
.interval-view > p { margin: 0; color: var(--color-ink-muted); font-size: 11px; }
.interval-row { display: grid; grid-template-columns: minmax(70px, 125px) minmax(48px, 1fr) auto; align-items: center; gap: 8px; min-height: 24px; }
.interval-label { overflow: hidden; color: var(--color-ink-soft); font-size: 11px; text-overflow: ellipsis; white-space: nowrap; }
.interval-track { position: relative; height: 12px; border-radius: 99px; background: var(--color-surface-subtle); }
.interval-track i { position: absolute; top: 4px; height: 4px; border-radius: 99px; background: var(--color-teal); }
.interval-track b { position: absolute; top: 1px; width: 10px; height: 10px; border: 2px solid #fff; border-radius: 50%; background: var(--color-navy); transform: translateX(-50%); box-shadow: 0 0 0 1px var(--color-navy); }
.interval-value { color: var(--color-ink-muted); font-size: 10px; font-variant-numeric: tabular-nums; white-space: nowrap; }
.panel-state { display: grid; min-height: 180px; place-content: center; justify-items: center; gap: 10px; color: var(--color-ink-muted); text-align: center; font-size: 13px; }
.panel-state p { max-width: 50ch; margin: 0; line-height: 1.6; }
.panel-state--error { color: var(--color-danger); }
.loader { width: 22px; height: 22px; border: 2px solid var(--color-border); border-top-color: var(--color-teal); border-radius: 50%; animation: spin .8s linear infinite; }
.limitations { margin: 0; padding-left: 18px; color: var(--color-orange); font-size: 12px; line-height: 1.55; }
.method-details { padding-top: 8px; border-top: 1px solid var(--color-border); }
.method-details summary { width: fit-content; min-height: 28px; color: var(--color-ink-muted); font-size: 11px; font-weight: 700; cursor: pointer; }
.method-details p { max-width: 68ch; margin: 5px 0 0; color: var(--color-ink-muted); font-size: 11px; line-height: 1.55; }
@keyframes spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .loader { animation: none; } }
@media (max-width: 560px) { .analysis-chart { padding: 14px; } .analysis-chart__heading { flex-direction: column; } .chart-actions { justify-content: flex-start; } .chart-canvas { height: 230px; } }
</style>
