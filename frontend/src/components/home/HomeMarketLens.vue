<template>
  <section class="market-lens" aria-labelledby="home-market-title">
    <div class="market-lens__intro">
      <h2 id="home-market-title">先看价格，<br>再决定看什么。</h2>
      <p class="market-lens__copy">以当前数据快照中的真实挂牌价，观察不同预算段的车源分布。每根柱子对应一个等宽价格区间。</p>
      <div class="market-lens__budgets" role="group" aria-label="选择预算范围">
        <button v-for="option in budgets" :key="option.id" type="button" :aria-pressed="selectedBudget === option.id" @click="choose(option.id)">{{ option.label }}</button>
      </div>
      <p class="market-lens__stat-label">{{ selectedLabel }} · 价格中位数</p>
      <p class="market-lens__median" aria-live="polite">{{ medianText }}</p>
      <p v-if="result" class="market-lens__sample">{{ number(result.counts.valid_rows) }} 条有效挂牌价 <span>／</span> 范围内 {{ number(result.counts.scope_rows) }} 条</p>
      <p v-else-if="state === 'loading'" class="market-lens__sample">正在读取真实挂牌样本…</p>
    </div>

    <div class="market-lens__viz">
      <p class="market-lens__viz-heading">挂牌价分布 <span>车源数 · 按价格区间</span></p>
      <p v-if="state === 'loading'" class="market-lens__message" role="status">正在读取当前数据快照…</p>
      <div v-else-if="state === 'error'" class="market-lens__message" role="alert">
        <span>{{ errorMessage }}</span><button type="button" @click="load">重试</button>
      </div>
      <p v-else-if="state === 'unavailable'" class="market-lens__message" role="status">数据能力摘要暂不可用，无法确认当前快照。</p>
      <p v-else-if="state === 'empty'" class="market-lens__message" role="status">这个预算范围内没有可展示的挂牌价样本。</p>
      <template v-else-if="result && bars.length">
        <div class="market-lens__chart-scroll" role="region" aria-label="挂牌价分布图，可横向滚动查看">
        <svg class="market-lens__chart" viewBox="0 0 720 300" role="img" :aria-label="chartDescription" preserveAspectRatio="xMidYMid meet">
          <g class="market-lens__grid"><line v-for="tick in 4" :key="tick" x1="58" :y1="plotY(tick - 1)" x2="704" :y2="plotY(tick - 1)" /><text v-for="tick in 4" :key="`t${tick}`" x="48" :y="plotY(tick - 1) + 4" text-anchor="end">{{ number(Math.round(maxCount * (4 - tick) / 3)) }}</text></g>
          <g v-for="(bar, index) in bars" :key="`${bar.lower_yuan}-${bar.upper_yuan}`">
            <rect :x="barX(index)" :y="barY(bar.count)" :width="barWidth" :height="Math.max(0, chartBottom - barY(bar.count))" rx="2" :class="['market-lens__bar', { 'market-lens__bar--median': index === medianBinIndex }]">
              <title>{{ formatWan(bar.lower_yuan) }}–{{ formatWan(bar.upper_yuan) }} 万元：{{ number(bar.count) }} 条</title>
            </rect>
          </g>
          <g class="market-lens__axis"><text x="58" y="286">{{ formatWan(bars[0].lower_yuan) }}</text><text x="704" y="286" text-anchor="end">{{ formatWan(bars[bars.length - 1].upper_yuan) }} 万元</text><text x="381" y="299" text-anchor="middle">挂牌价（万元）</text></g>
        </svg>
        </div>
        <p class="market-lens__scroll-hint">在窄屏上可横向滚动查看图表。</p>
        <div class="market-lens__legend"><span><i aria-hidden="true"></i>样本数量</span><span class="market-lens__legend-note">深色区间包含中位数</span></div>
        <p class="market-lens__note">区间为等宽分箱；柱高表示各区间内挂牌车源数，不表示时间变化。<span v-if="selectedBudget === 'all'">全样本中的高价长尾会拉宽价格区间，可切换预算查看细分。</span></p>
        <details class="market-lens__details"><summary>查看分箱数据</summary><div class="market-lens__table-wrap"><table><caption>挂牌价分箱及车源数</caption><thead><tr><th scope="col">挂牌价区间</th><th scope="col">车源数</th><th scope="col">占有效价格样本</th></tr></thead><tbody><tr v-for="bin in bars" :key="`row-${bin.lower_yuan}-${bin.upper_yuan}`"><td>{{ formatWan(bin.lower_yuan) }}–{{ formatWan(bin.upper_yuan) }} 万元</td><td>{{ number(bin.count) }}</td><td>{{ percent(bin.share) }}</td></tr></tbody></table></div></details>
      </template>
      <p v-else class="market-lens__message" role="status">等待数据能力摘要…</p>
      <button v-if="state === 'success'" class="market-lens__action" type="button" @click="emit('select', selectedQuery)">把这个范围写入问题 <span aria-hidden="true">↗</span></button>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import type { AnalysisResult, DashboardFilters } from '../../types/dashboard'
import { emptyDashboardFilters } from '../../types/dashboard'
import { getAnalysis } from '../../services/dashboardAdapter'
import type { DataCapabilities } from '../../services/analysisAdapter'

type BudgetId = 'all' | 'under100' | '100to200'
interface PriceBin { lower_yuan: number; upper_yuan: number; count: number; share: number; include_upper?: boolean }
interface PriceData { n: number; median_yuan: number | null; bins: PriceBin[] }
const props = defineProps<{ capabilities: DataCapabilities | null; capabilitiesLoaded: boolean }>()
const emit = defineEmits<{ select: [query: string] }>()
const budgets: Array<{ id: BudgetId; label: string }> = [
  { id: 'all', label: '全部样本' }, { id: 'under100', label: '10 万以内' }, { id: '100to200', label: '10—20 万' },
]
const selectedBudget = ref<BudgetId>('100to200')
const state = ref<'waiting' | 'loading' | 'success' | 'empty' | 'error' | 'unavailable'>('waiting')
const result = ref<AnalysisResult<PriceData> | null>(null)
const errorMessage = ref('读取市场样本失败，请稍后重试。')
let controller: AbortController | null = null
let sequence = 0
const selectedLabel = computed(() => budgets.find((item) => item.id === selectedBudget.value)?.label ?? '全部样本')
const medianText = computed(() => result.value?.data.median_yuan != null ? `${formatMedianWan(result.value.data.median_yuan)} 万元` : state.value === 'loading' ? '读取中' : '—')
const bars = computed(() => result.value?.data.bins ?? [])
const maxCount = computed(() => Math.max(1, ...bars.value.map((bin) => bin.count)))
const chartBottom = 255
const chartTop = 20
const chartHeight = chartBottom - chartTop
const barWidth = computed(() => Math.max(2, 646 / Math.max(1, bars.value.length) - 3))
const chartDescription = computed(() => `挂牌价分布直方图，${number(result.value?.counts.valid_rows ?? 0)} 条有效挂牌价；横轴为挂牌价万元，纵轴为车源数。`)
const medianBinIndex = computed(() => {
  const median = result.value?.data.median_yuan
  if (median == null) return -1
  return bars.value.findIndex((bin, index) => median >= bin.lower_yuan && (median < bin.upper_yuan || (index === bars.value.length - 1 && median <= bin.upper_yuan)))
})
const selectedQuery = computed(() => selectedBudget.value === 'all'
  ? '请基于当前全部二手车挂牌样本，分析挂牌价分布并说明样本量与中位数。'
  : selectedBudget.value === 'under100'
    ? '请分析挂牌价在 10 万元以内的二手车挂牌样本分布，并说明样本量与中位数。'
    : '请分析挂牌价在 10 万至 20 万元之间的二手车挂牌样本分布，并说明样本量与中位数。')

function number(value: number): string { return new Intl.NumberFormat('zh-CN').format(value) }
function formatWan(yuan: number): string { return Number((yuan / 10_000).toFixed(1)).toString() }
function formatMedianWan(yuan: number): string { return Number((yuan / 10_000).toFixed(2)).toString() }
function percent(value: number): string { return `${(value * 100).toFixed(1)}%` }
function barX(index: number): number { return 58 + index * (646 / Math.max(1, bars.value.length)) + 1 }
function plotY(level: number): number { return 20 + level * (235 / 3) }
function barY(count: number): number { return chartBottom - count / maxCount.value * chartHeight }

function isPriceData(value: unknown): value is PriceData {
  if (!value || typeof value !== 'object') return false
  const data = value as Record<string, unknown>
  return typeof data.n === 'number' && Number.isFinite(data.n) &&
    (data.median_yuan === null || (typeof data.median_yuan === 'number' && Number.isFinite(data.median_yuan))) &&
    Array.isArray(data.bins) && data.bins.every((item: unknown) => {
      if (!item || typeof item !== 'object') return false
      const bin = item as Record<string, unknown>
      return typeof bin.lower_yuan === 'number' && Number.isFinite(bin.lower_yuan) &&
        typeof bin.upper_yuan === 'number' && Number.isFinite(bin.upper_yuan) &&
        typeof bin.count === 'number' && Number.isFinite(bin.count) && bin.count >= 0 &&
        typeof bin.share === 'number' && Number.isFinite(bin.share)
    })
}

function choose(id: BudgetId): void { if (selectedBudget.value !== id) selectedBudget.value = id }
async function load(): Promise<void> {
  const current = ++sequence
  controller?.abort()
  controller = null
  result.value = null
  if (!props.capabilitiesLoaded) { state.value = 'waiting'; return }
  if (!props.capabilities?.dataset_version) { state.value = 'unavailable'; return }
  const filters: DashboardFilters = emptyDashboardFilters()
  if (selectedBudget.value === 'under100') filters.price_max_yuan = 100_000
  if (selectedBudget.value === '100to200') { filters.price_min_yuan = 100_000; filters.price_max_yuan = 200_000 }
  const request = new AbortController()
  controller = request
  state.value = 'loading'
  try {
    const response = await getAnalysis('price_distribution', filters, props.capabilities.dataset_version, request.signal)
    if (current !== sequence || request.signal.aborted) return
    if (response.status === 'empty') {
      state.value = 'empty'
    } else if (response.status !== 'success') {
      errorMessage.value = '当前数据快照无法生成价格分布，请重试。'
      state.value = 'error'
    } else if (isPriceData(response.data) && response.data.bins.length > 0 && response.counts.valid_rows > 0) {
      result.value = response as AnalysisResult<PriceData>
      state.value = 'success'
    } else {
      errorMessage.value = '当前数据快照返回了不完整的价格分布，请重试。'
      state.value = 'error'
    }
  } catch (error) {
    if (current !== sequence || request.signal.aborted) return
    errorMessage.value = error instanceof Error ? error.message : '读取市场样本失败，请稍后重试。'
    state.value = 'error'
  }
}
watch(() => [selectedBudget.value, props.capabilitiesLoaded, props.capabilities?.dataset_version] as const, () => { void load() }, { immediate: true })
onBeforeUnmount(() => { sequence++; controller?.abort(); controller = null })
</script>

<style scoped>
.market-lens { display: grid; grid-template-columns: minmax(240px, .8fr) minmax(0, 1.35fr); gap: clamp(36px, 7vw, 112px); padding: clamp(30px, 6vw, 82px) 0; color: #202821; border-top: 1px solid rgb(32 40 33 / .26); border-bottom: 1px solid rgb(32 40 33 / .26); }
.market-lens__intro { max-width: 390px; }.market-lens h2 { margin: 0; font-size: clamp(34px, 4.5vw, 58px); font-weight: 540; line-height: 1.15; letter-spacing: -.05em; }.market-lens__copy { max-width: 340px; margin: 20px 0 27px; color: #46534a; font-size: 13px; line-height: 1.8; }
.market-lens__budgets { display: flex; flex-wrap: wrap; gap: 7px; }.market-lens__budgets button { min-height: 44px; padding: 0 12px; color: #35453b; font: inherit; font-size: 12px; border: 1px solid rgb(32 40 33 / .28); border-radius: 2px; background: transparent; cursor: pointer; }.market-lens__budgets button[aria-pressed="true"] { color: #f6f3e9; border-color: #254d3b; background: #254d3b; }.market-lens button:focus-visible, .market-lens summary:focus-visible { outline: 3px solid #bb5739; outline-offset: 3px; }
.market-lens__stat-label { margin: 32px 0 2px; color: #46534a; font-size: 12px; }.market-lens__median { margin: 0; color: #254d3b; font-size: clamp(28px, 3vw, 42px); font-variant-numeric: tabular-nums; letter-spacing: -.04em; }.market-lens__sample { margin: 8px 0 0; color: #46534a; font-size: 12px; }.market-lens__sample span { padding: 0 5px; color: #69766d; }
.market-lens__viz { min-width: 0; }.market-lens__viz-heading { display: flex; justify-content: space-between; gap: 16px; margin: 4px 0 12px; padding-bottom: 12px; color: #27372e; font-size: 14px; border-bottom: 1px solid rgb(32 40 33 / .2); }.market-lens__viz-heading span { color: #46534a; font-size: 12px; }.market-lens__chart-scroll { max-width: 100%; overflow-x: auto; overscroll-behavior-inline: contain; }.market-lens__chart { display: block; width: 100%; height: auto; aspect-ratio: 12 / 5; overflow: visible; }.market-lens__grid line { stroke: rgb(32 40 33 / .18); stroke-dasharray: 2 5; }.market-lens__grid text, .market-lens__axis text { fill: #46534a; font: 11px sans-serif; }.market-lens__bar { fill: #9eafa2; }.market-lens__bar--median { fill: #254d3b; }.market-lens__legend { display: flex; justify-content: space-between; color: #46534a; font-size: 12px; }.market-lens__legend span:first-child { display: flex; align-items: center; gap: 6px; }.market-lens__legend i { width: 9px; height: 9px; background: #9eafa2; }.market-lens__legend-note { color: #46534a; }.market-lens__note { margin: 12px 0 0; color: #46534a; font-size: 12px; line-height: 1.6; }.market-lens__scroll-hint { display: none; margin: 4px 0 0; color: #46534a; font-size: 12px; }.market-lens__details { margin-top: 16px; color: #35453b; font-size: 12px; }.market-lens__details summary { display: flex; width: fit-content; min-height: 44px; align-items: center; cursor: pointer; }.market-lens__table-wrap { max-height: 220px; overflow: auto; margin-top: 8px; }.market-lens table { width: 100%; border-collapse: collapse; text-align: left; }.market-lens caption { text-align: left; padding: 5px 0; }.market-lens th, .market-lens td { padding: 8px; border-bottom: 1px solid rgb(32 40 33 / .18); font-size: 12px; font-weight: 400; }.market-lens th { color: #46534a; }.market-lens__message { display: flex; min-height: 220px; align-items: center; justify-content: center; flex-wrap: wrap; gap: 10px; color: #46534a; font-size: 13px; text-align: center; }.market-lens__message button { min-height: 44px; padding: 8px 13px; color: white; border: 0; background: #254d3b; cursor: pointer; }.market-lens__action { display: inline-flex; min-height: 44px; gap: 10px; align-items: center; margin-top: 18px; padding: 0 0; color: #254d3b; font: inherit; font-size: 12px; border: 0; border-bottom: 1px solid #254d3b; background: none; cursor: pointer; }
@media (max-width: 700px) { .market-lens { grid-template-columns: 1fr; gap: 36px; }.market-lens__intro { max-width: none; }.market-lens__copy { max-width: 480px; }.market-lens__stat-label { margin-top: 22px; }.market-lens__chart { width: 560px; max-width: none; }.market-lens__grid text, .market-lens__axis text { font-size: 14px; }.market-lens__scroll-hint { display: block; } }
@media (max-width: 390px) { .market-lens__viz-heading { align-items: flex-start; flex-direction: column; gap: 4px; } }
</style>
