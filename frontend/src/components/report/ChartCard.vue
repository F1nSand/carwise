<template>
  <section class="chart-card" :aria-labelledby="titleId">
    <div class="chart-card__header">
      <div>
        <p class="chart-card__eyebrow">{{ unit }}</p>
        <h3 :id="titleId">{{ title }}</h3>
      </div>
      <button v-if="series.length" class="table-toggle" type="button" :aria-expanded="showTable" @click="showTable = !showTable">
        <Table2 :size="14" aria-hidden="true" /> {{ showTable ? '收起数据表' : '查看数据表' }}
      </button>
    </div>
    <p class="chart-card__summary">{{ summary }}</p>
    <div v-if="series.length && !showTable" ref="chartElement" class="chart" role="img" :aria-label="summary"></div>
    <div v-else class="chart-empty" data-chart-empty role="status">暂无可展示数据</div>
    <div v-if="showTable && series.length" class="chart-table-wrap">
      <table>
        <caption>{{ title }}数据表</caption>
        <thead><tr><th scope="col">项目</th><th scope="col">数值（{{ unit }}）</th></tr></thead>
        <tbody><tr v-for="datum in series" :key="datum.label"><th scope="row">{{ datum.label }}</th><td>{{ datum.value }}</td></tr></tbody>
      </table>
    </div>
  </section>
</template>

<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { Table2 } from 'lucide-vue-next'
import type { ChartDatum } from '../../types/analysis'
import type { ECharts } from 'echarts/core'

const props = defineProps<{
  title: string
  unit: string
  summary: string
  series: ChartDatum[]
  chartType?: 'bar' | 'line'
  color?: string
}>()

const chartElement = ref<HTMLDivElement | null>(null)
const showTable = ref(false)
const titleId = `chart-title-${Math.random().toString(36).slice(2, 8)}`
let chart: ECharts | null = null
let chartResizeObserver: ResizeObserver | null = null
let renderVersion = 0

function disposeChart() {
  chartResizeObserver?.disconnect()
  chartResizeObserver = null
  chart?.dispose()
  chart = null
}

async function renderChart() {
  const currentRenderVersion = ++renderVersion
  if (!chartElement.value) {
    disposeChart()
    return
  }
  if (props.series.length === 0) {
    disposeChart()
    return
  }
  const [{ use, init }, { BarChart, LineChart }, { GridComponent, TooltipComponent }, { SVGRenderer }] = await Promise.all([
    import('echarts/core'),
    import('echarts/charts'),
    import('echarts/components'),
    import('echarts/renderers'),
  ])
  if (currentRenderVersion !== renderVersion || !chartElement.value) return
  use([BarChart, LineChart, GridComponent, TooltipComponent, SVGRenderer])
  disposeChart()
  chart = init(chartElement.value, undefined, { renderer: 'svg' })
  chart.setOption({
    animationDuration: 360,
    animationEasing: 'cubicOut',
    grid: { top: 16, right: 18, bottom: 30, left: 44, containLabel: true },
    tooltip: { trigger: 'axis', valueFormatter: (value: unknown) => `${value} ${props.unit}` },
    xAxis: { type: 'category', data: props.series.map((datum) => datum.label), axisLine: { lineStyle: { color: '#cbd5e1' } }, axisLabel: { color: '#64748b', fontSize: 11, hideOverlap: true, formatter: (value: string) => value.length > 10 ? `${value.slice(0, 9)}…` : value } },
    yAxis: { type: 'value', name: props.unit, nameTextStyle: { color: '#64748b', fontSize: 10 }, splitLine: { lineStyle: { color: '#e2e8f0' } }, axisLabel: { color: '#64748b', fontSize: 10 } },
    series: [{ type: props.chartType ?? 'bar', data: props.series.map((datum) => datum.value), barMaxWidth: 34, smooth: true, itemStyle: { color: props.color ?? '#0f766e', borderRadius: [7, 7, 0, 0] }, lineStyle: { color: props.color ?? '#0f766e', width: 3 }, areaStyle: props.chartType === 'line' ? { color: 'rgba(15, 118, 110, 0.10)' } : undefined }],
  })
  chartResizeObserver = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(() => chart?.resize())
  chartResizeObserver?.observe(chartElement.value)
  chart.resize()
}

onMounted(async () => {
  await nextTick()
  renderChart()
})

watch(() => [props.series, props.chartType, props.color, props.unit, showTable.value], renderChart, { deep: true, flush: 'post' })

onBeforeUnmount(() => {
  renderVersion += 1
  disposeChart()
})
</script>

<style scoped>
.chart-card {
  min-width: 0;
  padding: 20px;
  border: 1px solid var(--color-border);
  border-radius: 16px;
  background: var(--color-surface);
  box-shadow: none;
}

.chart-card__header { display: flex; align-items: flex-start; justify-content: space-between; gap: 14px; }
.chart-card__eyebrow { margin: 0 0 5px; color: var(--color-ink-muted); font-family: var(--font-mono); font-size: 12px; letter-spacing: 0.02em; }
.chart-card h3 { margin: 0; color: var(--color-navy); font-size: 17px; letter-spacing: -0.035em; }
.chart-card__summary { min-height: 38px; margin: 12px 0 0; color: var(--color-ink-muted); font-size: 13px; line-height: 1.6; }
.chart { width: 100%; max-width: 100%; height: 260px; margin-top: 10px; min-width: 0; }
.chart-empty { display: grid; min-height: 160px; margin-top: 10px; place-items: center; color: var(--color-ink-muted); border: 1px dashed var(--color-border); border-radius: 12px; background: var(--color-surface-subtle); font-size: 13px; }
.table-toggle { display: inline-flex; align-items: center; gap: 5px; min-height: 40px; padding: 0 11px; color: var(--color-teal); border: 1px solid rgb(15 118 110 / 0.2); border-radius: 8px; background: var(--color-teal-soft); font-size: 12px; font-weight: 700; white-space: nowrap; }
@media (hover: hover) and (pointer: fine) { .table-toggle:hover { border-color: var(--color-teal); } }
.chart-table-wrap { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; color: var(--color-ink-soft); font-size: 12px; }
caption { margin-bottom: 8px; color: var(--color-ink-muted); text-align: left; }
th, td { padding: 8px; text-align: left; border-bottom: 1px solid var(--color-border); }
thead th { color: var(--color-ink-muted); font-size: 12px; font-weight: 600; }
tbody th { color: var(--color-navy); font-weight: 650; }

@media (max-width: 560px) {
  .chart-card__header { align-items: flex-start; flex-direction: column; }
  .chart { height: 230px; }
}
</style>
