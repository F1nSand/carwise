<template>
  <div class="data-file">
    <section id="overview" class="overview section-anchor" aria-labelledby="overview-label">
      <div id="overview-label" class="overview__label">当前数据概况</div>
      <div v-if="status === 'loading'" class="overview__loading" role="status" aria-live="polite">
        <span class="loading-mark" aria-hidden="true"></span>
        <span>正在读取当前数据概况…</span>
      </div>
      <div v-else-if="status === 'error'" class="overview__error" role="alert">
        <div><strong>暂时无法读取数据说明</strong><p>{{ errorMessage }}</p></div>
        <button type="button" @click="loadCapabilities">重新读取</button>
      </div>
      <div v-else-if="capabilities" class="overview__body">
        <div class="overview__main">
          <h2 id="overview-title"><span>{{ formattedRows }}</span><small>条挂牌记录</small></h2>
          <p>数据版本 <code :title="capabilities.dataset_version">{{ capabilities.dataset_version.slice(0, 12) }}</code></p>
        </div>
        <dl class="overview__facts">
          <div><dt>能源字段覆盖</dt><dd>{{ coverageLabel('energy_type') }}</dd></div>
          <div><dt>数据采集时间</dt><dd>{{ timeRangeLabel }}</dd></div>
        </dl>
      </div>
      <p v-if="status === 'ready'" class="overview__footnote">数据为历史挂牌快照；样本数量不代表当前在售数量或成交量。</p>
    </section>

    <section id="coverage" class="record-section section-anchor" aria-labelledby="coverage-title">
      <div class="section-heading"><span>01 / 字段</span><h2 id="coverage-title">哪些信息有记录</h2></div>
      <div v-if="status === 'ready' && coverageRows.length" class="coverage-content">
        <div class="coverage-list" aria-label="字段覆盖情况">
          <div v-for="field in coverageRows" :key="field.key" class="coverage-row">
            <span class="coverage-row__name">{{ field.label }}</span>
            <div class="coverage-row__track" role="img" :aria-label="`${field.label}覆盖 ${field.labelValue}`">
              <span :style="{ width: `${field.percent}%` }"></span>
            </div>
            <strong>{{ field.labelValue }}</strong>
          </div>
        </div>
        <p class="coverage-note">覆盖率表示该字段在记录中有值的比例。能源字段未填写时只表示信息缺失，不能据此判断车辆是燃油车；覆盖率也不代表数据准确率。</p>
      </div>
      <div v-else-if="status === 'loading'" class="section-placeholder" aria-hidden="true"></div>
      <div v-else-if="status === 'error'" class="section-muted">数据读取失败，字段覆盖暂不可用。</div>
      <div v-else class="section-muted">当前接口没有返回字段覆盖信息。</div>
    </section>

    <section id="filters" class="record-section section-anchor" aria-labelledby="filters-title">
      <div class="section-heading"><span>02 / 条件</span><h2 id="filters-title">可筛选与暂缺条件</h2></div>
      <div v-if="status === 'ready' && capabilities" class="filter-columns">
        <div><h3>可用条件</h3><ul class="term-list"><li v-for="item in supportedLabels" :key="item">{{ item }}</li></ul></div>
        <div><h3>暂不支持</h3><ul v-if="unsupportedLabels.length" class="term-list term-list--muted"><li v-for="item in unsupportedLabels" :key="item">{{ item }}</li></ul><p v-else class="inline-note">当前接口未列出暂不支持的筛选条件。</p></div>
      </div>
      <p v-if="status === 'ready' && capabilities?.energy_values.length" class="energy-note"><span>能源枚举</span>{{ capabilities.energy_values.join('、') }}</p>
      <div v-else-if="status === 'loading'" class="section-placeholder section-placeholder--short" aria-hidden="true"></div>
      <div v-else-if="status === 'error'" class="section-muted">筛选条件暂不可用。</div>
    </section>

    <section id="method" class="record-section section-anchor" aria-labelledby="method-title">
      <div class="section-heading"><span>03 / 方法</span><h2 id="method-title">问题如何变成结果</h2></div>
      <ol class="method-list">
        <li><span>01</span><div><h3>理解问题</h3><p>模型识别你关心的车型、预算和比较方向；条件不清楚时会先询问。</p></div></li>
        <li><span>02</span><div><h3>调用分析工具</h3><p>系统按确认后的条件筛选记录，并用确定性的统计工具计算结果。不同看板和 Agent 工具可用的条件各有范围。</p></div></li>
        <li><span>03</span><div><h3>说明证据与限制</h3><p>回答引用工具返回的样本和统计结果。样本不足或条件不受支持时，应说明限制；具体结果仍需结合车辆实况判断。</p></div></li>
      </ol>
      <dl class="method-notes">
        <div><dt>价格含义</dt><dd>页面中的价格均为挂牌价。中位数描述样本中间位置，不能直接当作某辆车的成交估价。</dd></div>
        <div><dt>比较口径</dt><dd>比较时同时查看筛选范围、样本量和价格分布。各分析的异常值处理与样本门槛，以具体图表或报告说明为准。</dd></div>
      </dl>
    </section>

    <section id="limits" class="record-section limits section-anchor" aria-labelledby="limits-title">
      <div class="section-heading"><span>04 / 边界</span><h2 id="limits-title">这些数据不能说明什么</h2></div>
      <ul class="limits-list">
        <li>挂牌记录不等于成交价格，也不能代表实时市场。</li>
        <li>采集时间范围未知时，无法判断记录对应的具体日期。</li>
        <li>字段缺失不代表某种车辆类别；可用的筛选条件以当前数据版本为准。</li>
        <li>统计结果不能替代车况检查、事故核验或专业估价。</li>
      </ul>
      <div class="faq-list">
        <details><summary>能源字段没填，是不是就是燃油车？</summary><p>不是。未填写只代表这条记录缺少能源信息，不能据此推断能源类型。</p></details>
        <details><summary>字段覆盖率高，结果就一定准确吗？</summary><p>不是。覆盖率只说明字段有值的比例，不衡量字段值是否准确，也不保证样本足以回答每个问题。</p></details>
        <details><summary>为什么有些条件不能筛选？</summary><p>缺少足够可靠的字段时，系统不能确认这些条件。请查看上方的“暂不支持”列表；每个分析工具可使用的条件也可能不同。</p></details>
      </div>
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { getDataCapabilities, type DataCapabilities } from '../../services/analysisAdapter'

const capabilities = ref<DataCapabilities | null>(null)
const status = ref<'loading' | 'ready' | 'error'>('loading')
const errorMessage = ref('请检查连接后重试。')
let controller: AbortController | null = null
let requestId = 0

const labels: Record<string, string> = {
  price_yuan: '挂牌价格', mileage_km: '行驶里程', production_year: '生产年份',
  vehicle_age_years: '记录车龄', city_name: '车源城市', brand_name: '品牌',
  row_count: '挂牌记录', price: '价格', brand: '品牌', series: '车系', city: '城市',
  year: '生产年份', mileage: '里程', energy_type: '能源类型', body_type: '车型类别',
  drive_type: '驱动形式', displacement: '排量', transmission: '变速箱', max_age: '车龄',
  budget: '预算',
}
const filterLabels: Record<string, string> = {
  budget: '预算', city: '城市', brand: '品牌', series: '车系', year: '生产年份',
  energy_type: '能源类型', max_age: '车龄', body_type: '车型类别', drive_type: '驱动形式',
}
const formattedRows = computed(() => capabilities.value?.row_count.toLocaleString('zh-CN') ?? '')
const timeRangeLabel = computed(() => {
  const range = capabilities.value?.data_time_range
  if (range?.status !== 'known') return '未知'
  if (range.start && range.end) return `${range.start} 至 ${range.end}`
  return range.start || range.end || '未知'
})
const coverageRows = computed(() => Object.entries(capabilities.value?.fields ?? {}).map(([key, value]) => {
  const percent = Math.max(0, Math.min(100, value.coverage * 100))
  return { key, label: labels[key] || key, percent, labelValue: `${percent.toFixed(2)}%` }
}))
const supportedLabels = computed(() => capabilities.value?.supported_filters.map((key) => filterLabels[key] || key) ?? [])
const unsupportedLabels = computed(() => capabilities.value?.unsupported_filters.map((key) => filterLabels[key] || key) ?? [])

function coverageLabel(key: string): string {
  const coverage = capabilities.value?.fields[key]?.coverage
  return typeof coverage === 'number' ? `${(coverage * 100).toFixed(2)}%` : '未提供'
}

async function loadCapabilities(): Promise<void> {
  controller?.abort()
  const nextController = new AbortController()
  controller = nextController
  const currentRequest = ++requestId
  status.value = 'loading'
  try {
    const result = await getDataCapabilities(nextController.signal)
    if (nextController.signal.aborted || currentRequest !== requestId) return
    capabilities.value = result
    status.value = 'ready'
  } catch (error) {
    if (nextController.signal.aborted || currentRequest !== requestId) return
    errorMessage.value = error instanceof Error ? error.message : '读取失败，请检查连接后重试。'
    status.value = 'error'
  }
}

onMounted(() => { void loadCapabilities() })
onUnmounted(() => { requestId += 1; controller?.abort() })
</script>

<style scoped>
.data-file { color: #202821; }
.section-anchor { scroll-margin-top: calc(var(--topbar-height, 76px) + 24px); }
.overview { min-height: 232px; padding: 24px 28px 20px; background: #254d3b; color: #fff; }
.overview__label { color: rgb(255 255 255 / .72); font-size: 12px; letter-spacing: .04em; }
.overview__body { display: grid; grid-template-columns: minmax(0, 1.2fr) minmax(220px, .8fr); align-items: end; gap: 28px; margin-top: 20px; }
.overview__main h2 { display: flex; flex-wrap: wrap; align-items: baseline; gap: 10px; margin: 0; font-size: clamp(42px, 6vw, 70px); font-weight: 550; line-height: 1; letter-spacing: -.06em; }
.overview__main h2 small { font-size: 16px; font-weight: 450; letter-spacing: 0; }
.overview__main p { margin: 14px 0 0; color: rgb(255 255 255 / .78); font-size: 13px; }
.overview code { color: inherit; font-family: var(--font-mono, monospace); font-size: 12px; overflow-wrap: anywhere; }
.overview__facts { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin: 0; padding-top: 12px; border-top: 1px solid rgb(255 255 255 / .28); }
.overview__facts dt { color: rgb(255 255 255 / .72); font-size: 12px; }
.overview__facts dd { margin: 7px 0 0; font-size: 16px; font-weight: 600; overflow-wrap: anywhere; }
.overview__footnote { margin: 18px 0 0; color: rgb(255 255 255 / .72); font-size: 12px; }
.overview__loading { display: flex; min-height: 126px; align-items: center; gap: 12px; color: rgb(255 255 255 / .82); font-size: 14px; }
.loading-mark { width: 8px; height: 8px; border-radius: 50%; background: #c9d7c7; }
.overview__error { display: flex; min-height: 126px; align-items: center; justify-content: space-between; gap: 18px; }
.overview__error strong { font-size: 18px; }
.overview__error p { margin: 8px 0 0; color: rgb(255 255 255 / .76); font-size: 13px; overflow-wrap: anywhere; }
.overview__error button { min-height: 44px; padding: 0 14px; border: 1px solid rgb(255 255 255 / .55); border-radius: 2px; background: transparent; color: white; font: inherit; cursor: pointer; }
.record-section { padding: 30px 0 32px; border-bottom: 1px solid #cfd3ca; }
.section-heading { display: grid; grid-template-columns: 110px minmax(0, 1fr); align-items: baseline; gap: 12px; margin-bottom: 22px; }
.section-heading > span { color: #636e64; font-size: 11px; letter-spacing: .05em; }
.section-heading h2 { margin: 0; color: #202821; font-size: clamp(21px, 3vw, 28px); font-weight: 550; letter-spacing: -.04em; }
.coverage-content { display: grid; grid-template-columns: minmax(0, 1.2fr) minmax(210px, .8fr); gap: 36px; align-items: start; }
.coverage-list { display: grid; gap: 0; }
.coverage-row { display: grid; grid-template-columns: minmax(100px, 150px) minmax(60px, 1fr) 58px; align-items: center; gap: 14px; min-height: 38px; border-bottom: 1px solid #e1e2dc; font-size: 13px; }
.coverage-row__name { color: #505c53; }
.coverage-row__track { height: 5px; background: #e1e2dc; }
.coverage-row__track span { display: block; height: 100%; background: #254d3b; }
.coverage-row strong { text-align: right; font-variant-numeric: tabular-nums; }
.coverage-note { margin: 0; color: #505c53; font-size: 13px; line-height: 1.75; }
.section-placeholder { height: 124px; border-top: 1px solid #dedfd7; border-bottom: 1px solid #dedfd7; }
.section-placeholder--short { height: 70px; }
.section-muted, .inline-note { color: #636e64; font-size: 13px; }
.filter-columns { display: grid; grid-template-columns: 1fr 1fr; gap: 32px; }
.filter-columns h3, .method-list h3 { margin: 0 0 12px; font-size: 14px; font-weight: 650; }
.term-list { display: flex; flex-wrap: wrap; gap: 8px 16px; margin: 0; padding: 0; list-style: none; }
.term-list li { padding: 3px 0; border-bottom: 1px solid #254d3b; color: #254d3b; font-size: 13px; }
.term-list--muted li { border-color: #bac2b7; color: #636e64; }
.inline-note { margin: 0; }
.energy-note { display: flex; flex-wrap: wrap; gap: 8px 16px; margin: 20px 0 0; padding-top: 14px; border-top: 1px solid #dedfd7; color: #505c53; font-size: 13px; }
.energy-note span { color: #636e64; }
.method-list { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 0 24px; margin: 0; padding: 0; list-style: none; }
.method-list li { display: grid; grid-template-columns: 32px 1fr; gap: 10px; padding: 16px 0; border-top: 1px solid #cfd3ca; }
.method-list li > span { color: #254d3b; font-size: 12px; }
.method-list h3 { margin-bottom: 7px; }
.method-list p { margin: 0; color: #505c53; font-size: 13px; line-height: 1.7; }
.method-notes { display: grid; gap: 14px; margin: 18px 0 0; padding-top: 18px; border-top: 1px solid #cfd3ca; }
.method-notes > div { display: grid; grid-template-columns: 88px minmax(0, 1fr); gap: 12px; }
.method-notes dt { font-size: 13px; font-weight: 600; }
.method-notes dd { margin: 0; color: #505c53; font-size: 13px; line-height: 1.7; }
.limits-list { display: grid; gap: 10px; margin: 0 0 24px; padding: 0; list-style: none; }
.limits-list li { position: relative; padding-left: 16px; color: #505c53; font-size: 13px; line-height: 1.65; }
.limits-list li::before { position: absolute; top: .65em; left: 0; width: 5px; height: 5px; background: #a74b30; content: ''; }
.faq-list { border-top: 1px solid #cfd3ca; }
.faq-list details { border-bottom: 1px solid #cfd3ca; }
.faq-list summary { min-height: 48px; display: flex; align-items: center; justify-content: space-between; gap: 12px; color: #202821; font-size: 14px; font-weight: 600; cursor: pointer; list-style: none; }
.faq-list summary::-webkit-details-marker { display: none; }
.faq-list summary::after { color: #254d3b; content: '+'; font-size: 18px; }
.faq-list details[open] summary::after { content: '−'; }
.faq-list details p { max-width: 760px; margin: 0 0 16px; color: #505c53; font-size: 13px; line-height: 1.7; }
button:focus-visible, summary:focus-visible { outline: 2px solid #c39b50; outline-offset: 3px; }
@media (max-width: 760px) {
  .overview { padding: 22px 20px 18px; }
  .overview__body { grid-template-columns: 1fr; gap: 20px; margin-top: 18px; }
  .overview__facts { max-width: 480px; }
  .coverage-content { grid-template-columns: 1fr; gap: 18px; }
  .method-list { grid-template-columns: 1fr; }
  .method-list li { grid-template-columns: 36px 1fr; }
}
@media (max-width: 480px) {
  .overview { min-height: 0; padding: 20px 16px; }
  .overview__main h2 { font-size: clamp(40px, 12vw, 54px); }
  .overview__facts { grid-template-columns: 1fr 1fr; gap: 12px; }
  .overview__facts dd { font-size: 14px; }
  .record-section { padding: 25px 0 27px; }
  .section-heading { grid-template-columns: 1fr; gap: 5px; margin-bottom: 17px; }
  .coverage-row { grid-template-columns: minmax(90px, 1fr) minmax(50px, 1fr) 52px; gap: 9px; font-size: 12px; }
  .filter-columns { grid-template-columns: 1fr; gap: 22px; }
  .overview__error { align-items: flex-start; flex-direction: column; padding: 18px 0; }
}
</style>
