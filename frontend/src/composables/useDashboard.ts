import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { DashboardRequestError, getAnalysis, getDashboard, getDashboardCatalog, parseFilters, serializeFilters } from '../services/dashboardAdapter'
import { emptyDashboardFilters, toDashboardDraft, toDashboardFilters, type AnalysisResult, type AnalysisType, type DashboardCatalog, type DashboardDraft, type DashboardFilters, type DashboardResponse } from '../types/dashboard'

export function validateDashboardFilters(filters: DashboardFilters, catalog: DashboardCatalog): string | null {
  if (filters.brands.length > 20 || filters.cities.length > 20) return '品牌和城市各最多选择 20 项。'
  if (filters.series_selectors.length > 6) return '车系最多选择 6 项。'
  if (filters.brands.some((item) => !catalog.brands.includes(item))) return '所选品牌已不在当前数据目录中，请刷新目录。'
  if (filters.cities.some((item) => !catalog.cities.includes(item))) return '所选城市已不在当前数据目录中，请刷新目录。'
  if (filters.energy_types.some((item) => !catalog.energy_types.includes(item))) return '所选能源类型不受当前数据支持。'
  if (filters.series_selectors.some((item) => !catalog.series_selectors.some((entry) => entry.brand_name === item.brand_name && entry.series === item.series) || (filters.brands.length > 0 && !filters.brands.includes(item.brand_name)))) return '车系必须与当前品牌选择匹配，请调整筛选。'
  const ranges: Array<[string, number | null, number | null, boolean]> = [
    ['预算', filters.price_min_yuan, filters.price_max_yuan, false], ['里程', filters.mileage_min_km, filters.mileage_max_km, false],
    ['生产年份', filters.year_min, filters.year_max, true], ['车龄', filters.age_min_years, filters.age_max_years, false],
  ]
  for (const [label, lower, upper, integer] of ranges) {
    for (const value of [lower, upper]) if (value !== null && (!Number.isFinite(value) || value < 0 || (integer && !Number.isInteger(value)))) return `${label}范围必须是有效的非负${integer ? '整数' : '数值'}。`
    if (lower !== null && upper !== null && lower > upper) return `${label}下限不能大于上限。`
  }
  return null
}

function cloneDraft(draft: DashboardDraft): DashboardDraft {
  return { ...draft, brands: [...draft.brands], seriesSelectors: draft.seriesSelectors.map((item) => ({ ...item })), cities: [...draft.cities], energyTypes: [...draft.energyTypes] }
}

export function useDashboard() {
  const route = useRoute()
  const router = useRouter()
  const dashboardPath = route.path
  const catalog = ref<DashboardCatalog | null>(null)
  const draft = ref<DashboardDraft>(toDashboardDraft(emptyDashboardFilters()))
  const appliedFilters = ref<DashboardFilters>(emptyDashboardFilters())
  const response = ref<DashboardResponse | null>(null)
  const loading = ref(false)
  const catalogLoading = ref(false)
  const error = ref('')
  const notice = ref('')
  const validationError = ref('')
  const cardLoading = ref<AnalysisType | null>(null)
  const cardErrors = ref<Partial<Record<AnalysisType, string>>>({})
  const cardResults = ref<Partial<Record<AnalysisType, AnalysisResult>>>({})
  let queryController: AbortController | null = null
  let catalogController: AbortController | null = null
  let querySequence = 0
  let catalogSequence = 0
  function cancelRequests() {
    querySequence += 1
    catalogSequence += 1
    queryController?.abort()
    catalogController?.abort()
  }
  // Route transitions briefly retain the outgoing page. Its async work must
  // stop as soon as navigation commits, rather than waiting for unmount.
  watch(() => route.path, (path) => { if (path !== dashboardPath) cancelRequests() }, { flush: 'sync' })

  const appliedCount = computed(() => appliedFilters.value.brands.length + appliedFilters.value.series_selectors.length + appliedFilters.value.cities.length + appliedFilters.value.energy_types.length + [appliedFilters.value.price_min_yuan, appliedFilters.value.price_max_yuan, appliedFilters.value.mileage_min_km, appliedFilters.value.mileage_max_km, appliedFilters.value.year_min, appliedFilters.value.year_max, appliedFilters.value.age_min_years, appliedFilters.value.age_max_years].filter((value) => value !== null).length)

  async function loadCatalog(): Promise<DashboardCatalog | null> {
    const seq = ++catalogSequence
    catalogController?.abort()
    catalogController = new AbortController()
    catalogLoading.value = true
    try {
      const nextCatalog = await getDashboardCatalog(catalogController.signal)
      if (seq !== catalogSequence) return null
      catalog.value = nextCatalog
      return nextCatalog
    } catch (reason) {
      if (seq === catalogSequence && !catalogController.signal.aborted) error.value = reason instanceof Error ? reason.message : '分析目录读取失败。'
      return null
    } finally {
      if (seq === catalogSequence) catalogLoading.value = false
    }
  }

  function restoreFromUrl(currentCatalog: DashboardCatalog) {
    const raw = route.query.filters
    const restored = parseFilters(Array.isArray(raw) ? raw[0] : raw, currentCatalog)
    if (restored) {
      appliedFilters.value = restored
      draft.value = toDashboardDraft(restored)
    } else {
      appliedFilters.value = currentCatalog.defaults?.filters ?? emptyDashboardFilters()
      draft.value = toDashboardDraft(appliedFilters.value)
      if (raw !== undefined) notice.value = '链接中的筛选已失效，已恢复为当前数据默认条件。'
    }
  }

  async function apply() {
    if (route.path !== dashboardPath) return
    const currentCatalog = catalog.value
    if (!currentCatalog) { error.value = '分析目录尚未就绪，请刷新后重试。'; return }
    const filters = toDashboardFilters(cloneDraft(draft.value))
    validationError.value = validateDashboardFilters(filters, currentCatalog) || ''
    if (validationError.value) return
    queryController?.abort()
    const controller = new AbortController()
    queryController = controller
    const seq = ++querySequence
    appliedFilters.value = filters
    error.value = ''
    response.value = null
    cardResults.value = {}
    cardErrors.value = {}
    cardLoading.value = null
    loading.value = true
    try {
      await router.replace({ query: { ...route.query, filters: serializeFilters(filters) } })
      if (route.path !== dashboardPath || seq !== querySequence || controller.signal.aborted) return
      const result = await getDashboard(filters, currentCatalog.dataset_version, controller.signal)
      if (seq !== querySequence) return
      if (result.dataset_version !== currentCatalog.dataset_version) throw new DashboardRequestError('数据版本已更新，请刷新目录后重新应用筛选。', 'dataset_changed', 409)
      response.value = result
    } catch (reason) {
      if (seq !== querySequence || controller.signal.aborted) return
      error.value = reason instanceof Error ? reason.message : '看板查询失败。'
      if (reason instanceof DashboardRequestError && reason.code === 'dataset_changed') {
        notice.value = error.value
        await loadCatalog()
      }
    } finally {
      if (seq === querySequence) loading.value = false
    }
  }

  async function retry(type: AnalysisType) {
    if (route.path !== dashboardPath) return
    if (!catalog.value) return
    const resultVersion = response.value?.dataset_version ?? catalog.value.dataset_version
    queryController?.abort()
    const controller = new AbortController()
    queryController = controller
    const seq = ++querySequence
    cardLoading.value = type
    cardErrors.value = { ...cardErrors.value, [type]: '' }
    try {
      const result = await getAnalysis(type, appliedFilters.value, resultVersion, controller.signal)
      if (seq !== querySequence) return
      if (result.dataset_version !== resultVersion) throw new DashboardRequestError('数据版本已更新，请刷新目录后重新应用筛选。', 'dataset_changed', 409)
      cardResults.value = { ...cardResults.value, [type]: result }
      if (response.value) response.value = { ...response.value, analyses: { ...response.value.analyses, [type]: result } }
    } catch (reason) {
      if (seq !== querySequence || controller.signal.aborted) return
      const message = reason instanceof Error ? reason.message : '单项分析重试失败。'
      cardErrors.value = { ...cardErrors.value, [type]: message }
      if (reason instanceof DashboardRequestError && reason.code === 'dataset_changed') {
        notice.value = message
        response.value = null
        cardResults.value = {}
        const freshCatalog = await loadCatalog()
        if (freshCatalog) await apply()
      }
    } finally {
      if (seq === querySequence) cardLoading.value = null
    }
  }

  function reset() {
    draft.value = toDashboardDraft(catalog.value?.defaults.filters ?? emptyDashboardFilters())
    validationError.value = ''
    notice.value = ''
  }

  async function initialize() {
    const currentCatalog = await loadCatalog()
    if (!currentCatalog || route.path !== dashboardPath) return
    restoreFromUrl(currentCatalog)
    await apply()
  }

  watch(() => route.query.filters, (raw, previous) => {
    if (route.path !== dashboardPath || !catalog.value || raw === previous) return
    const restored = parseFilters(Array.isArray(raw) ? raw[0] : raw, catalog.value)
    const nextFilters = raw === undefined ? catalog.value.defaults.filters : restored
    if (nextFilters && JSON.stringify(nextFilters) !== JSON.stringify(appliedFilters.value)) {
      draft.value = toDashboardDraft(nextFilters)
      appliedFilters.value = nextFilters
      void apply()
    } else if (!nextFilters && raw !== undefined) notice.value = '链接中的筛选已失效，已保留当前条件。'
  })

  onBeforeUnmount(cancelRequests)

  return { catalog, draft, appliedFilters, response, loading, catalogLoading, error, notice, validationError, cardLoading, cardErrors, cardResults, appliedCount, loadCatalog, initialize, apply, retry, reset }
}
