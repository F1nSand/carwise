<template>
  <form class="filters" @submit.prevent="submit">
    <div class="filters__heading"><div><h2>筛选车源</h2><p>修改条件后点击“应用筛选”，图表不会随输入即时变化。</p></div><button class="reset" type="button" @click="emit('reset')">重置</button></div>

    <div class="field-grid">
      <div class="field field--wide"><span>品牌 <small>最多 20 项</small></span><SearchMultiSelect v-model="model.brands" :options="catalog.brands" label="品牌" :limit="20" /></div>
      <div class="field field--wide series-field">
        <span>车系 <small>品牌与车系联合匹配，最多 6 项</small></span>
        <div class="series-add">
          <label><span class="sr-only">选择车系品牌</span><select v-model="seriesBrand" aria-label="车系品牌"><option value="">先选品牌</option><option v-for="brand in seriesBrands" :key="brand" :value="brand">{{ brand }}</option></select></label>
          <label><span class="sr-only">搜索车系</span><input v-model="seriesSearch" type="search" aria-label="搜索车系" placeholder="搜索车系" list="dashboard-series-options" /><datalist id="dashboard-series-options"><option v-for="item in availableSeries" :key="item.series" :value="item.series" /></datalist></label>
          <button class="add-series" type="button" :disabled="!canAddSeries" @click="addSeries">添加</button>
        </div>
        <ul v-if="model.seriesSelectors.length" class="chips" aria-label="已选车系"><li v-for="item in model.seriesSelectors" :key="`${item.brand_name}:${item.series}`"><span>{{ item.brand_name }} · {{ item.series }}</span><button type="button" :aria-label="`移除${item.brand_name} ${item.series}`" @click="removeSeries(item)"><X :size="14" /></button></li></ul>
        <p v-else class="field-note">不选车系时保留所有车系。</p>
        <p v-if="removedSeriesCount" class="field-note field-note--warning" role="status">品牌变化后移除了 {{ removedSeriesCount }} 个不匹配的车系。</p>
      </div>

      <div class="field field--wide"><span>城市 <small>最多 20 项；按城市统计</small></span><SearchMultiSelect v-model="model.cities" :options="catalog.cities" label="城市" :limit="20" /></div>
      <div class="field"><span>挂牌预算 <small>万元</small></span><div class="range-inputs"><label><span class="sr-only">预算下限（万元）</span><input v-model.number="model.priceMinWan" type="number" min="0" step="0.1" placeholder="不限" /></label><span>至</span><label><span class="sr-only">预算上限（万元）</span><input v-model.number="model.priceMaxWan" type="number" min="0" step="0.1" placeholder="不限" /></label></div></div>
      <div class="field"><span>行驶里程 <small>万公里</small></span><div class="range-inputs"><label><span class="sr-only">里程下限（万公里）</span><input v-model.number="model.mileageMinWanKm" type="number" min="0" step="0.1" placeholder="不限" /></label><span>至</span><label><span class="sr-only">里程上限（万公里）</span><input v-model.number="model.mileageMaxWanKm" type="number" min="0" step="0.1" placeholder="不限" /></label></div></div>
      <div class="field"><span>生产年份 <small>年份范围</small></span><div class="range-inputs"><label><span class="sr-only">生产年份下限</span><input v-model.number="model.yearMin" type="number" :min="catalog.ranges.production_year?.min" :max="catalog.ranges.production_year?.max" step="1" placeholder="不限" /></label><span>至</span><label><span class="sr-only">生产年份上限</span><input v-model.number="model.yearMax" type="number" :min="catalog.ranges.production_year?.min" :max="catalog.ranges.production_year?.max" step="1" placeholder="不限" /></label></div></div>
      <div class="field"><span>数据快照车龄 <small>年</small></span><div class="range-inputs"><label><span class="sr-only">车龄下限（年）</span><input v-model.number="model.ageMinYears" type="number" min="0" step="0.1" placeholder="不限" /></label><span>至</span><label><span class="sr-only">车龄上限（年）</span><input v-model.number="model.ageMaxYears" type="number" min="0" step="0.1" placeholder="不限" /></label></div></div>
    </div>

    <details class="advanced"><summary>高级筛选：能源类型</summary><div class="field"><span>能源类型 <small>该字段覆盖约 {{ energyCoverage }}% 车源，缺失记录不视为燃油车</small></span><SearchMultiSelect v-model="model.energyTypes" :options="catalog.energy_types" label="能源类型" /></div></details>

    <p v-if="validationError" class="filter-error" role="alert">{{ validationError }}</p>
    <button class="apply" type="submit" :disabled="loading || catalogLoading"><Filter :size="16" aria-hidden="true" />{{ loading ? '正在查询…' : '应用筛选' }}</button>
  </form>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { Filter, X } from 'lucide-vue-next'
import SearchMultiSelect from './SearchMultiSelect.vue'
import type { DashboardCatalog, DashboardDraft, SeriesSelector } from '../../types/dashboard'

const model = defineModel<DashboardDraft>({ required: true })
const props = defineProps<{ catalog: DashboardCatalog; validationError?: string; loading?: boolean; catalogLoading?: boolean }>()
const emit = defineEmits<{ apply: []; reset: [] }>()
const seriesBrand = ref('')
const seriesSearch = ref('')
const removedSeriesCount = ref(0)
const seriesBrands = computed(() => model.value.brands.length ? model.value.brands : props.catalog.brands)
const availableSeries = computed(() => props.catalog.series_selectors.filter((item) => item.brand_name === seriesBrand.value))
const hasSeries = computed(() => model.value.seriesSelectors.some((item) => item.brand_name === seriesBrand.value && item.series === seriesSearch.value.trim()))
const canAddSeries = computed(() => !!seriesBrand.value && availableSeries.value.some((item) => item.series === seriesSearch.value.trim()) && !hasSeries.value && model.value.seriesSelectors.length < 6)
const energyCoverage = computed(() => Math.round((props.catalog.fields.energy_type?.coverage ?? 0) * 1000) / 10)

watch(seriesBrands, (brands) => { if (!brands.includes(seriesBrand.value)) seriesBrand.value = brands[0] ?? ''; seriesSearch.value = '' }, { immediate: true })
watch(() => model.value.brands.slice(), (brands) => {
  const before = model.value.seriesSelectors.length
  model.value.seriesSelectors = model.value.seriesSelectors.filter((item) => !brands.length || brands.includes(item.brand_name))
  removedSeriesCount.value = Math.max(0, before - model.value.seriesSelectors.length)
})
function addSeries() {
  const selectedName = seriesSearch.value.trim()
  if (!seriesBrand.value || !selectedName || !availableSeries.value.some((item) => item.series === selectedName) || hasSeries.value || model.value.seriesSelectors.length >= 6) return
  model.value.seriesSelectors = [...model.value.seriesSelectors, { brand_name: seriesBrand.value, series: selectedName }]
  seriesSearch.value = ''
}
function removeSeries(item: SeriesSelector) { model.value.seriesSelectors = model.value.seriesSelectors.filter((candidate) => candidate.brand_name !== item.brand_name || candidate.series !== item.series) }
function submit() { emit('apply') }
</script>

<style scoped>
.filters { display: grid; gap: 18px; padding: 20px; border: 1px solid var(--color-border); border-radius: 14px; background: var(--color-surface); }
.filters__heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }
.filters__heading h2 { margin: 0; color: var(--color-navy); font-size: 18px; letter-spacing: -.025em; }
.filters__heading p { margin: 5px 0 0; color: var(--color-ink-muted); font-size: 12px; line-height: 1.5; }
.reset { min-height: 40px; padding: 0 12px; color: var(--color-ink-soft); border: 1px solid var(--color-border-strong); border-radius: 9px; background: #fff; font-size: 12px; font-weight: 650; cursor: pointer; }
.field-grid { display: grid; grid-template-columns: minmax(0, 1fr); gap: 16px; }
.field { display: grid; min-width: 0; gap: 7px; color: var(--color-ink); font-size: 13px; font-weight: 650; }
.field > span { display: flex; align-items: baseline; flex-wrap: wrap; gap: 7px; }
.field small { color: var(--color-ink-muted); font-size: 11px; font-weight: 450; }
.field--wide { grid-column: 1 / -1; }
select, input { width: 100%; min-width: 0; min-height: 42px; padding: 8px 10px; color: var(--color-ink); border: 1px solid var(--color-border-strong); border-radius: 8px; background: var(--color-surface); font: inherit; font-size: 13px; }
select[multiple] { height: auto; min-height: 96px; padding: 4px; }
option { padding: 6px 8px; }
select:focus-visible, input:focus-visible, button:focus-visible, summary:focus-visible { outline: 3px solid rgb(15 118 110 / .32); outline-offset: 2px; }
.range-inputs { display: grid; grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr); align-items: center; gap: 7px; color: var(--color-ink-muted); }
.series-add { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }
.add-series { grid-column: 1 / -1; }
.add-series { min-height: 42px; padding: 0 12px; color: #fff; border: 0; border-radius: 8px; background: var(--color-navy); font-size: 12px; font-weight: 700; cursor: pointer; }
.add-series:disabled, .apply:disabled { opacity: .55; cursor: not-allowed; }
.chips { display: flex; flex-wrap: wrap; gap: 7px; margin: 1px 0 0; padding: 0; list-style: none; }
.chips li { display: flex; align-items: center; gap: 5px; max-width: 100%; padding: 2px 4px 2px 10px; color: var(--color-navy); border: 1px solid var(--color-border); border-radius: 999px; background: var(--color-surface-subtle); font-size: 11px; }
.chips li span { overflow-wrap: anywhere; }
.chips button { display: grid; width: 30px; height: 30px; flex: 0 0 30px; place-items: center; color: var(--color-ink-muted); border: 0; border-radius: 50%; background: transparent; cursor: pointer; }
.chips button:hover { color: var(--color-danger); background: var(--color-danger-soft); }
.field-note { margin: 0; color: var(--color-ink-muted); font-size: 11px; }
.field-note--warning, .filter-error { color: var(--color-orange); }
.advanced { padding-top: 14px; border-top: 1px solid var(--color-border); }
.advanced summary { width: fit-content; min-height: 36px; color: var(--color-navy); font-size: 13px; font-weight: 700; cursor: pointer; }
.advanced .field { margin-top: 12px; }
.filter-error { margin: 0; font-size: 13px; }
.apply { display: inline-flex; min-height: 46px; align-items: center; justify-content: center; gap: 8px; padding: 0 18px; color: #fff; border: 0; border-radius: 10px; background: var(--color-navy); font-size: 13px; font-weight: 750; cursor: pointer; }
.apply:hover:not(:disabled) { background: #164160; }
.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }
@media (max-width: 620px) { .filters { padding: 15px; } .field--wide { grid-column: auto; } .series-add { grid-template-columns: 1fr; } .apply { width: 100%; } }
</style>
