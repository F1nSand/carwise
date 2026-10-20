<template>
  <div class="report-page">
    <Transition name="state-fade" mode="out-in">
    <div v-if="loading" key="loading" class="report-state" role="status"><span class="loader" aria-hidden="true"></span><h1>正在打开报告</h1><p>正在读取结构化分析结果。</p></div>
    <div v-else-if="error" key="error" class="report-state report-state--error" role="alert"><AlertCircle :size="24" aria-hidden="true" /><h1>报告暂时不可用</h1><p>{{ error }}</p><RouterLink class="button button--primary" to="/">重新开始分析</RouterLink></div>
    <div v-else-if="report" :key="`report-${route.params.id}`" class="report-content">
      <ReportHeader :report="report" />
      <div class="report-layout">
        <div class="report-main" role="region" aria-label="报告内容">
          <template v-if="report.intent === 'recommend'">
            <InsightSummary :report="report" />
            <section class="report-section" aria-labelledby="recommendations-title">
              <div class="section-heading"><h2 id="recommendations-title">推荐车系</h2><span>{{ report.candidateCount }} 个车系通过样本门槛</span></div>
              <div class="recommendation-grid"><RecommendationCard v-for="(recommendation, index) in report.recommendations" :key="recommendation.series" :recommendation="recommendation" :rank="index + 1" /></div>
            </section>
            <section class="report-section" aria-labelledby="recommendation-chart-title">
              <div class="section-heading"><h2 id="recommendation-chart-title">价格与样本</h2><span>中位挂牌价 / 样本量</span></div>
              <div class="chart-grid"><ChartCard title="车系价格对比" unit="万元" :series="report.priceChart" summary="三款优先车系的中位挂牌价与预算上沿接近程度。" color="#c2410c" /><ChartCard title="车系样本量" unit="条" :series="report.sampleChart" summary="样本量越充足，车系层面的价格结论越稳定。" /></div>
            </section>
          </template>

          <template v-else-if="report.intent === 'price_compare'">
            <InsightSummary :report="report" />
            <section class="report-section" aria-labelledby="market-chart-title">
              <div class="section-heading"><h2 id="market-chart-title">价格分布与变化</h2><span>单位：万元</span></div>
              <div class="chart-grid"><ChartCard title="不同年份的中位挂牌价" unit="万元" chart-type="line" :series="report.yearChart" summary="横截面样本显示，近年车源的中位挂牌价整体更高。" /><ChartCard title="城市中位挂牌价" unit="万元" :series="report.cityChart" summary="城市之间存在价差，建议结合车源数量和运输成本判断。" color="#c2410c" /></div>
            </section>
            <section class="insight-note"><Gauge :size="17" aria-hidden="true" /><p><strong>里程价格影响：</strong>{{ report.mileageInsight }}</p></section>
            <RepresentativeListings :listings="report.representativeListings" />
          </template>

          <template v-else-if="report.intent === 'evidence'">
            <EvidenceMetaCard :report="report" />
            <AgentAnswerCard :report="report" />
          </template>

          <template v-else-if="report.intent === 'no_data'">
            <AgentAnswerCard :report="report" />
            <section v-if="report.relaxationCandidates.length" class="report-section no-data-options" aria-labelledby="no-data-options-title">
              <div class="section-heading"><h2 id="no-data-options-title">可以怎样继续</h2><span>由工具返回的有限放宽建议</span></div>
              <ul><li v-for="candidate in report.relaxationCandidates" :key="`${candidate.field}-${candidate.action}`">{{ candidate.label || `${candidate.field}：${candidate.action}` }}<span v-if="candidate.auto">可由 Agent 自动尝试</span><span v-else>需要你确认</span></li></ul>
            </section>
          </template>

          <AgentAnswerCard v-else :report="report" />

          <section v-if="report.analysisBlocks?.length" class="report-section report-analysis" aria-labelledby="report-analysis-title">
            <div class="section-heading"><h2 id="report-analysis-title">共享数据分析</h2><span>统计口径与市场看板一致</span></div>
            <div class="analysis-blocks"><AnalysisChart v-for="block in report.analysisBlocks" :key="`${block.analysis_type}-${block.dataset_version}`" :type="block.analysis_type" :title="analysisTitle(block.analysis_type)" :subtitle="`数据版本 ${block.dataset_version}`" :result="block" /></div>
          </section>

          <section v-if="report.finalPlan?.length" class="report-section report-plan" aria-labelledby="final-plan-title">
            <div class="section-heading"><h2 id="final-plan-title">本次执行计划</h2><span>{{ report.finalPlan.filter((step) => step.status === 'complete' || step.status === 'skipped').length }} / {{ report.finalPlan.length }} 步已收口</span></div>
            <ol><li v-for="step in report.finalPlan" :key="step.id"><strong>{{ step.title }}</strong><span>{{ step.status === 'skipped' ? '已跳过' : step.status === 'complete' ? '已完成' : step.status === 'running' ? '进行中' : '未执行' }}</span><small v-if="step.reason">{{ step.reason }}</small></li></ol>
          </section>

          <section class="next-action"><div><h2>把报告带回你的决策</h2><p>如果你已经有更具体的预算、城市或里程条件，可以回到原会话修改问题，再由你决定何时发送。</p></div><RouterLink class="button button--primary" :to="reanalyzeTarget">重新分析 <ArrowUpRight :size="16" aria-hidden="true" /></RouterLink></section>
        </div>
        <TrustRail :report="report" />
      </div>
    </div>
    </Transition>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { AlertCircle, ArrowUpRight, Gauge } from 'lucide-vue-next'
import AgentAnswerCard from '../components/report/AgentAnswerCard.vue'
import AnalysisChart from '../components/dashboard/AnalysisChart.vue'
import ChartCard from '../components/report/ChartCard.vue'
import EvidenceMetaCard from '../components/report/EvidenceMetaCard.vue'
import InsightSummary from '../components/report/InsightSummary.vue'
import RecommendationCard from '../components/report/RecommendationCard.vue'
import ReportHeader from '../components/report/ReportHeader.vue'
import RepresentativeListings from '../components/report/RepresentativeListings.vue'
import TrustRail from '../components/report/TrustRail.vue'
import { getReport } from '../services/analysisAdapter'
import type { AnalysisReport } from '../types/analysis'
import type { AnalysisType } from '../types/dashboard'

const route = useRoute()
const report = ref<AnalysisReport | null>(null)
const loading = ref(true)
const error = ref('')
const reanalyzeTarget = computed(() => {
  const current = report.value
  const query: Record<string, string> = {}
  if (current?.question) query.draft = current.question
  if (current?.id && current.conversationId) query.source_report_id = current.id
  return current?.conversationId
    ? { name: 'conversation', params: { id: current.conversationId }, query }
    : { name: 'chat', query }
})
const analysisTitles: Record<AnalysisType, string> = {
  price_distribution: '挂牌价分布', mileage_price_correlation: '里程与价格相关性', model_comparison: '车系横向对比',
  brand_profile: '品牌画像', region_gap: '城市挂牌价差', age_price_relation: '快照车龄与价格',
}
function analysisTitle(type: AnalysisType) { return analysisTitles[type] }

async function loadReport() {
  loading.value = true
  error.value = ''
  try {
    report.value = await getReport(String(route.params.id))
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : '报告读取失败。'
  } finally {
    loading.value = false
  }
}

onMounted(loadReport)
watch(() => route.params.id, loadReport)
</script>

<style scoped>
.report-page { display: grid; gap: 28px; min-width: 0; }
.report-layout { display: grid; grid-template-areas: 'main rail'; grid-template-columns: minmax(0, 1fr) 310px; align-items: start; gap: 24px; }
.report-main { display: grid; grid-area: main; gap: 24px; min-width: 0; }
.report-plan ol { display: grid; gap: 8px; margin: 0; padding: 0; list-style: none; }
.report-plan li { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 3px 12px; padding: 11px 13px; border: 1px solid var(--color-border); border-radius: 10px; background: var(--color-surface-subtle); }
.report-plan li strong { color: var(--color-navy); font-size: 13px; }
.report-plan li span { color: var(--color-teal); font-size: 12px; }
.report-plan li small { grid-column: 1 / -1; color: var(--color-ink-muted); font-size: 12px; }
.trust-rail { grid-area: rail; }
.report-section { display: grid; gap: 16px; }
.analysis-blocks { display: grid; gap: 14px; }
.section-heading { display: flex; align-items: end; justify-content: space-between; gap: 16px; }
.section-heading h2 { margin: 0; color: var(--color-navy); font-size: 20px; letter-spacing: -0.035em; }
.section-heading > span { color: var(--color-ink-muted); font-size: 12px; }
.recommendation-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 14px; }
.chart-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }
.insight-note { display: flex; gap: 10px; padding: 16px 18px; border: 1px solid rgb(194 65 12 / 0.2); border-radius: 14px; background: var(--color-orange-soft); }
.insight-note svg { flex: 0 0 auto; margin-top: 2px; color: var(--color-orange); }
.insight-note p { margin: 0; color: var(--color-ink-soft); font-size: 13px; line-height: 1.7; }
.insight-note strong { color: var(--color-navy); }
.next-action { display: flex; align-items: center; justify-content: space-between; gap: 20px; padding: 22px; border-radius: 16px; background: var(--color-teal-soft); }
.next-action h2 { margin: 0; color: var(--color-navy); font-size: 18px; letter-spacing: -0.035em; }
.next-action p:last-child { max-width: 540px; margin: 8px 0 0; color: var(--color-ink-muted); font-size: 14px; line-height: 1.65; }
.button { display: inline-flex; align-items: center; justify-content: center; gap: 8px; min-height: 44px; padding: 0 16px; border-radius: 11px; font-size: 13px; font-weight: 750; white-space: nowrap; }
.button--primary { color: #fff; background: var(--color-navy); }
.report-state { display: grid; min-height: 360px; place-items: center; align-content: center; gap: 10px; padding: 24px; color: var(--color-ink-soft); text-align: center; overflow-wrap: anywhere; }
.report-state h1 { margin: 8px 0 0; color: var(--color-navy); font-size: 26px; letter-spacing: -0.05em; }
.report-state p { margin: 0; font-size: 13px; }
.report-state--error svg { color: var(--color-danger); }
.report-state--error .button { margin-top: 10px; }
.loader { width: 28px; height: 28px; border: 3px solid var(--color-border); border-top-color: var(--color-teal); border-radius: 50%; animation: spin 800ms linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .loader { animation: none; } }

@media (max-width: 1080px) {
  .report-layout { grid-template-columns: minmax(0, 1fr) 280px; }
  .recommendation-grid { grid-template-columns: 1fr; }
}

@media (max-width: 980px) {
  .report-layout { grid-template-areas: 'main' 'rail'; grid-template-columns: 1fr; }
}

@media (max-width: 650px) {
  .chart-grid { grid-template-columns: 1fr; }
  .section-heading, .next-action { align-items: flex-start; flex-direction: column; }
  .next-action .button { width: 100%; }
}
</style>
