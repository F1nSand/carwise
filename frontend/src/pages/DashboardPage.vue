<template>
  <section class="dashboard-page page-container">
    <header class="page-heading"><div><h1>二手车市场看板</h1><p>按品牌、车系、预算与使用条件筛选真实挂牌样本，查看六项统计结果。</p></div><button v-if="catalog" class="refresh" type="button" :disabled="catalogLoading" @click="refreshCatalog"><RefreshCw :size="15" :class="{ spin: catalogLoading }" />刷新数据目录</button></header>

    <p v-if="notice" class="notice" role="status"><Info :size="16" />{{ notice }}</p>
    <p v-if="error" class="error-banner" role="alert"><AlertCircle :size="16" />{{ error }}<button v-if="!loading" type="button" @click="apply">重试看板</button></p>

    <section v-if="catalogLoading && !catalog" class="page-state" role="status"><span class="loader"></span><p>正在读取真实数据目录…</p></section>
    <section v-else-if="!catalog" class="page-state page-state--error"><AlertCircle :size="24" /><h2>分析目录暂时不可用</h2><p>{{ error || '请检查分析服务后重试。' }}</p><button type="button" @click="initialize">重新加载</button></section>
    <template v-else>
      <div class="dashboard-layout" :class="{ 'dashboard-layout--collapsed': filtersCollapsed }">
        <aside ref="filterSidebar" class="filter-sidebar" aria-label="看板筛选">
          <button class="filter-toggle" type="button" aria-controls="dashboard-filter-controls" :aria-expanded="!filtersCollapsed" :aria-label="filtersCollapsed ? '展开筛选条件' : '收起筛选条件'" @click="toggleFilters">
            <PanelLeftClose v-if="!filtersCollapsed" :size="17" aria-hidden="true" /><PanelLeftOpen v-else :size="17" aria-hidden="true" />
            <span v-if="!filtersCollapsed">收起筛选</span><span v-else class="sr-only">展开筛选</span>
          </button>
          <div id="dashboard-filter-controls" v-show="!filtersCollapsed" class="filter-controls">
            <DashboardFilters v-model="draft" :catalog="catalog" :loading="loading" :catalog-loading="catalogLoading" :validation-error="validationError" @apply="apply" @reset="resetAndApply" />
          </div>
          <p v-if="filtersCollapsed" class="filter-count" aria-hidden="true">{{ appliedCount }}</p>
        </aside>
        <div ref="dashboardMain" class="dashboard-main">
          <section v-if="appliedConditions.length" class="applied-conditions" aria-label="已应用筛选条件">
            <div class="applied-conditions__top"><strong>已应用条件</strong><span v-if="hasDraftChanges">筛选已修改，尚未应用</span></div>
            <div class="applied-conditions__chips"><button v-for="item in appliedConditions" :key="item.id" type="button" :aria-label="`移除筛选：${item.label}`" @click="removeCondition(item.kind, item.value)">{{ item.label }} <X :size="13" aria-hidden="true" /></button></div>
          </section>
          <p v-else-if="hasDraftChanges" class="unapplied-note" role="status">筛选已修改，尚未应用。</p>
          <section v-if="loading" class="loading-state" role="status"><span class="loader"></span><span>正在计算当前筛选的分析结果…</span></section>
          <template v-else-if="response">
            <DashboardSummary :overview="response.overview" :dataset-version="response.dataset_version" :description="summaryDescription" />
            <div class="analysis-heading"><div><h2>分析结果</h2><p>所有分析使用同一数据版本；各项按自身有效字段统计。</p></div></div>
            <section v-if="visibleAnalysisCards.length" class="analysis-grid" aria-label="可用分析结果">
              <DashboardPanel v-for="(item, index) in visibleAnalysisCards" :key="item.type" :class="{ 'analysis-grid__wide': item.type === 'age_price_relation' || (visibleAnalysisCards.length % 2 === 1 && index === visibleAnalysisCards.length - 1) }" :type="item.type" :title="item.title" :subtitle="item.subtitle" :result="response.analyses[item.type] ?? null" :loading="cardLoading === item.type" :error="cardErrors[item.type] || analysisError(response.analyses[item.type])" @retry="retry" />
            </section>
            <section v-else class="empty-results" role="status"><div><h2>当前条件下没有可展示的分析</h2><p>清空筛选后重新查看完整数据范围。</p></div><button type="button" @click="resetAndApply">清空筛选并重试</button></section>
            <details class="disclosure"><summary>统计口径与限制</summary><p>价格是挂牌价，车源样本不等同于成交市场。相关性与城市价差不能证明因果，混合车系结果可能受车型构成和快照车龄影响。车龄采用数据记录的快照值；采集日期范围未知，不构成按时间变化的趋势。能源字段仅覆盖部分车源。</p></details>
          </template>
        </div>
      </div>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { AlertCircle, Info, PanelLeftClose, PanelLeftOpen, RefreshCw, X } from 'lucide-vue-next'
import DashboardFilters from '../components/dashboard/DashboardFilters.vue'
import DashboardPanel from '../components/dashboard/DashboardPanel.vue'
import DashboardSummary from '../components/dashboard/DashboardSummary.vue'
import { useDashboard } from '../composables/useDashboard'
import { toDashboardDraft, type AnalysisType } from '../types/dashboard'
import { buildAnalysisView } from '../components/dashboard/charts/analysisView'

const state = useDashboard()
const { catalog, draft, appliedFilters, response, loading, catalogLoading, error, notice, validationError, cardLoading, cardErrors, appliedCount, apply, retry, reset } = state
const filtersCollapsed = ref(false)
const filterSidebar = ref<HTMLElement | null>(null)
const dashboardMain = ref<HTMLElement | null>(null)
const analysisCards: Array<{ type: AnalysisType; title: string; subtitle: string }> = [
  { type: 'price_distribution', title: '挂牌价分布', subtitle: '各价位车源数、样本中位数与分箱区间。' },
  { type: 'mileage_price_correlation', title: '里程与价格相关性', subtitle: '完整配对计算系数；散点采用稳定抽样展示。' },
  { type: 'model_comparison', title: '车系横向对比', subtitle: '挂牌价区间与中位数，保留低样本标记。' },
  { type: 'brand_profile', title: '品牌画像', subtitle: '当前筛选内有效车源的品牌份额。' },
  { type: 'region_gap', title: '城市挂牌价差', subtitle: '城市中位价相对当前基准的差值。' },
  { type: 'age_price_relation', title: '快照车龄与价格', subtitle: '使用车源记录车龄，不由生产年份推算。' },
]
const visibleAnalysisCards = computed(() => analysisCards.filter(({ type }) => {
  const result = response.value?.analyses[type]
  if (!result) return false
  if (result.status === 'error') return true
  if (result.status !== 'success') return false
  const view = buildAnalysisView(result)
  return !!view.option || !!view.intervals?.length
}))
const summaryDescription = computed(() => `当前数据快照内筛选后 ${new Intl.NumberFormat('zh-CN').format(response.value?.overview.scope_rows ?? 0)} 条车源。`)
const appliedConditions = computed(() => {
  const filters = appliedFilters.value
  const items: Array<{ id: string; label: string; kind: string; value: unknown }> = []
  filters.brands.forEach((value) => items.push({ id: `brand:${value}`, label: value, kind: 'brand', value }))
  filters.series_selectors.forEach((value) => items.push({ id: `series:${value.brand_name}:${value.series}`, label: `${value.brand_name} · ${value.series}`, kind: 'series', value }))
  filters.cities.forEach((value) => items.push({ id: `city:${value}`, label: value, kind: 'city', value }))
  filters.energy_types.forEach((value) => items.push({ id: `energy:${value}`, label: value, kind: 'energy', value }))
  if (filters.price_min_yuan !== null || filters.price_max_yuan !== null) items.push({ id: 'price', label: `预算 ${wan(filters.price_min_yuan)}–${wan(filters.price_max_yuan)} 万元`, kind: 'price', value: null })
  if (filters.mileage_min_km !== null || filters.mileage_max_km !== null) items.push({ id: 'mileage', label: `里程 ${wan(filters.mileage_min_km)}–${wan(filters.mileage_max_km)} 万公里`, kind: 'mileage', value: null })
  if (filters.year_min !== null || filters.year_max !== null) items.push({ id: 'year', label: `生产年份 ${range(filters.year_min, filters.year_max)}`, kind: 'year', value: null })
  if (filters.age_min_years !== null || filters.age_max_years !== null) items.push({ id: 'age', label: `车龄 ${range(filters.age_min_years, filters.age_max_years)} 年`, kind: 'age', value: null })
  return items
})
const hasDraftChanges = computed(() => JSON.stringify(draft.value) !== JSON.stringify(toDashboardDraft(appliedFilters.value)))
let flipAnimations: Animation[] = []
let reduceMotionQuery: MediaQueryList | null = null
onMounted(() => {
  filtersCollapsed.value = typeof window.matchMedia === 'function' && window.matchMedia('(max-width: 900px)').matches
  if (typeof window.matchMedia === 'function') {
    reduceMotionQuery = window.matchMedia('(prefers-reduced-motion: reduce)')
    reduceMotionQuery.addEventListener?.('change', onMotionPreferenceChange)
  }
  void state.initialize()
})
onBeforeUnmount(() => {
  reduceMotionQuery?.removeEventListener?.('change', onMotionPreferenceChange)
  reduceMotionQuery = null
  cancelFlipAnimations()
})
async function initialize() { await state.initialize() }
async function resetAndApply() { reset(); await apply() }
function removeCondition(kind: string, value: unknown) {
  const filters = { ...appliedFilters.value, brands: [...appliedFilters.value.brands], series_selectors: appliedFilters.value.series_selectors.map((item) => ({ ...item })), cities: [...appliedFilters.value.cities], energy_types: [...appliedFilters.value.energy_types] }
  if (kind === 'brand') { filters.brands = filters.brands.filter((item) => item !== value); filters.series_selectors = filters.series_selectors.filter((item) => item.brand_name !== value) }
  else if (kind === 'series') filters.series_selectors = filters.series_selectors.filter((item) => item.brand_name !== (value as { brand_name: string }).brand_name || item.series !== (value as { series: string }).series)
  else if (kind === 'city') filters.cities = filters.cities.filter((item) => item !== value)
  else if (kind === 'energy') filters.energy_types = filters.energy_types.filter((item) => item !== value)
  else if (kind === 'price') { filters.price_min_yuan = null; filters.price_max_yuan = null }
  else if (kind === 'mileage') { filters.mileage_min_km = null; filters.mileage_max_km = null }
  else if (kind === 'year') { filters.year_min = null; filters.year_max = null }
  else if (kind === 'age') { filters.age_min_years = null; filters.age_max_years = null }
  draft.value = toDashboardDraft(filters)
  void apply()
}
function range(min: number | null, max: number | null) { return `${min ?? '不限'}–${max ?? '不限'}` }
function wan(value: number | null) { return value === null ? '不限' : new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 2 }).format(value / 10_000) }
async function refreshCatalog() {
  const freshCatalog = await state.loadCatalog()
  if (freshCatalog) {
    notice.value = '数据目录已刷新；请重新应用条件以查询当前版本。'
    if (!appliedCount.value) await apply()
  }
}
function analysisError(result: { status: string; reason_code: string | null } | null | undefined) {
  return result?.status === 'error' ? `此项分析暂时失败${result.reason_code ? `（原因代码：${result.reason_code}）` : ''}，可重试。` : ''
}
function cancelFlipAnimations() {
  flipAnimations.forEach((animation) => animation.cancel())
  flipAnimations = []
}
function onMotionPreferenceChange(event: MediaQueryListEvent) { if (event.matches) cancelFlipAnimations() }
async function toggleFilters() {
  cancelFlipAnimations()
  const sidebar = filterSidebar.value
  const main = dashboardMain.value
  const before = [sidebar, main].map((element) => element?.getBoundingClientRect())
  filtersCollapsed.value = !filtersCollapsed.value
  await nextTick()
  if (reduceMotionQuery?.matches || window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) return
  ;[sidebar, main].forEach((element, index) => {
    const first = before[index]
    const last = element?.getBoundingClientRect()
    if (!element || !first || !last) return
    const dx = first.left - last.left
    const dy = first.top - last.top
    const scaleX = index === 1 && last.width > 0 ? first.width / last.width : 1
    if (!dx && !dy && scaleX === 1) return
    if (typeof element.animate !== 'function') return
    const animation = element.animate([
      { transform: `translate(${dx}px, ${dy}px) scaleX(${scaleX})`, transformOrigin: 'left top' },
      { transform: 'translate(0, 0) scaleX(1)', transformOrigin: 'left top' },
    ], { duration: 220, easing: 'cubic-bezier(0.77, 0, 0.175, 1)' })
    flipAnimations.push(animation)
    const removeAnimation = () => { flipAnimations = flipAnimations.filter((item) => item !== animation) }
    animation.onfinish = removeAnimation
    animation.oncancel = removeAnimation
  })
}
</script>

<style scoped>
.dashboard-page { display: grid; gap: 18px; min-width: 0; padding-top: 24px; padding-bottom: 48px; }
.page-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 18px; }
h1 { margin: 0; color: var(--color-navy); font-size: clamp(25px, 3vw, 34px); letter-spacing: -.04em; }
.page-heading p { margin: 7px 0 0; color: var(--color-ink-muted); font-size: 14px; line-height: 1.6; }
.refresh { display: inline-flex; min-height: 42px; align-items: center; gap: 8px; padding: 0 13px; color: var(--color-navy); border: 1px solid var(--color-border-strong); border-radius: 9px; background: var(--color-surface); font-size: 12px; font-weight: 700; cursor: pointer; }
.refresh:disabled { opacity: .6; cursor: wait; }
.spin { animation: spin .8s linear infinite; }
.dashboard-layout { display: grid; grid-template-columns: 280px minmax(0, 1fr); align-items: start; gap: 20px; min-width: 0; }
.dashboard-layout--collapsed { grid-template-columns: 56px minmax(0, 1fr); }
.filter-sidebar { display: grid; min-width: 0; gap: 10px; }
.filter-toggle { display: inline-flex; min-height: 42px; align-items: center; justify-content: center; gap: 8px; padding: 0 12px; color: var(--color-navy); border: 1px solid var(--color-border); border-radius: 10px; background: var(--color-surface); font-size: 12px; font-weight: 700; cursor: pointer; }
.filter-toggle:focus-visible, .empty-results button:focus-visible, .query-error button:focus-visible, .disclosure summary:focus-visible { outline: 3px solid rgb(15 118 110 / .32); outline-offset: 2px; }
.dashboard-layout--collapsed .filter-toggle { width: 56px; padding: 0; }
.filter-count { margin: 0; color: var(--color-ink-muted); font-size: 11px; text-align: center; }
.filter-controls { min-width: 0; }
.dashboard-main { display: grid; min-width: 0; gap: 16px; }
.applied-conditions { display: grid; gap: 8px; padding: 13px 15px; border: 1px solid var(--color-border); border-radius: 12px; background: var(--color-surface); }
.applied-conditions__top { display: flex; align-items: baseline; justify-content: space-between; gap: 10px; }
.applied-conditions__top strong { color: var(--color-navy); font-size: 12px; }
.applied-conditions__top span, .unapplied-note { margin: 0; color: var(--color-orange); font-size: 11px; }
.applied-conditions__chips { display: flex; flex-wrap: wrap; gap: 6px; }
.applied-conditions__chips button { display: inline-flex; min-height: 34px; align-items: center; gap: 5px; max-width: 100%; padding: 0 9px; color: var(--color-navy); border: 1px solid var(--color-border); border-radius: 999px; background: var(--color-navy-soft); font-size: 11px; overflow-wrap: anywhere; cursor: pointer; }
.applied-conditions__chips button:hover { border-color: var(--color-teal); background: var(--color-teal-soft); }
.unapplied-note { padding: 0 3px; }
.analysis-heading { display: flex; align-items: end; justify-content: space-between; gap: 12px; }
.analysis-heading h2 { margin: 0; color: var(--color-navy); font-size: 20px; letter-spacing: -.025em; }
.analysis-heading p, .analysis-heading > span { margin: 5px 0 0; color: var(--color-ink-muted); font-size: 12px; line-height: 1.55; }
.analysis-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); align-items: stretch; gap: 14px; }
.analysis-grid__wide { grid-column: 1 / -1; }
.notice, .error-banner { display: flex; align-items: center; gap: 9px; margin: 0; padding: 11px 14px; border-radius: 10px; font-size: 13px; line-height: 1.5; }
.notice { color: var(--color-navy); border: 1px solid var(--color-border); background: var(--color-navy-soft); }
.error-banner { color: var(--color-danger); border: 1px solid rgb(180 35 24 / .2); background: var(--color-danger-soft); }
.error-banner button { min-height: 36px; margin-left: auto; padding: 0 10px; color: var(--color-danger); border: 1px solid rgb(180 35 24 / .3); border-radius: 8px; background: #fff; font-size: 12px; font-weight: 700; cursor: pointer; }
.disclosure { padding: 13px 15px; color: var(--color-ink-soft); border: 1px solid var(--color-border); border-radius: 12px; background: var(--color-surface-subtle); }
.disclosure summary { width: fit-content; min-height: 26px; color: var(--color-navy); font-size: 12px; font-weight: 700; cursor: pointer; }
.disclosure p { max-width: 90ch; margin: 8px 0 0; font-size: 12px; line-height: 1.65; }
.loading-state { display: flex; min-height: 190px; align-items: center; justify-content: center; gap: 12px; color: var(--color-ink-muted); border: 1px solid var(--color-border); border-radius: 12px; background: var(--color-surface); font-size: 13px; }
.loading-state .loader { width: 22px; height: 22px; }
.empty-results, .query-error { display: flex; align-items: center; justify-content: space-between; gap: 16px; padding: 22px; border: 1px solid var(--color-border); border-radius: 12px; background: var(--color-surface); }
.empty-results h2 { margin: 0; color: var(--color-navy); font-size: 16px; }
.empty-results p, .query-error p { margin: 5px 0 0; color: var(--color-ink-muted); font-size: 13px; }
.empty-results button, .query-error button { min-height: 42px; padding: 0 14px; color: white; border: 0; border-radius: 8px; background: var(--color-navy); font-size: 12px; font-weight: 700; cursor: pointer; }
.query-error { color: var(--color-danger); }
.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }
.page-state { display: grid; min-height: 320px; align-content: center; justify-items: center; gap: 10px; color: var(--color-ink-muted); text-align: center; }
.page-state h2, .page-state p { margin: 0; }
.page-state h2 { color: var(--color-navy); font-size: 20px; }
.page-state p { font-size: 13px; }
.page-state button { min-height: 42px; padding: 0 14px; color: white; border: 0; border-radius: 8px; background: var(--color-navy); font-weight: 700; cursor: pointer; }
.loader { width: 26px; height: 26px; border: 3px solid var(--color-border); border-top-color: var(--color-teal); border-radius: 50%; animation: spin .8s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .loader, .spin { animation: none; } }
@media (min-width: 1100px) { .filter-sidebar { position: sticky; top: 16px; } }
@media (max-width: 1099px) { .dashboard-layout, .dashboard-layout--collapsed { grid-template-columns: minmax(0, 1fr); gap: 12px; } .filter-sidebar { position: static; } .dashboard-layout--collapsed .filter-toggle { width: auto; justify-content: flex-start; } .filter-count { display: none; } }
@media (max-width: 780px) { .analysis-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (max-width: 560px) { .dashboard-page { gap: 14px; padding-top: 18px; } .page-heading { flex-direction: column; } .page-heading .refresh { width: 100%; justify-content: center; } .analysis-grid { grid-template-columns: 1fr; } .analysis-heading { align-items: flex-start; flex-direction: column; } .error-banner { align-items: flex-start; flex-wrap: wrap; } .error-banner button { margin-left: 0; } .empty-results, .query-error { align-items: flex-start; flex-direction: column; } }
</style>
