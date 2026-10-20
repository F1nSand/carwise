<template>
  <section class="insight-card" aria-labelledby="insight-title">
    <div class="insight-card__topline">
      <span class="insight-card__marker" aria-hidden="true"><Sparkles :size="17" /></span>
    </div>
    <h2 id="insight-title">{{ report.summaryTitle }}</h2>
    <AgentMarkdownContent class="insight-card__text" :source="report.summaryText" />
    <ReportReliabilitySummary :report="report" />

    <div v-if="report.intent === 'recommend'" class="metric-grid">
      <div class="metric"><span>预算内车源</span><strong>{{ formatNumber(report.budgetCount) }}</strong><small>条可比较样本</small></div>
      <div class="metric"><span>候选车系</span><strong>{{ report.candidateCount }}</strong><small>通过样本门槛</small></div>
      <div class="metric"><span>优先候选</span><strong>{{ report.recommendations[0]?.series }}</strong><small>综合分 {{ formatScore(report.recommendations[0]?.score ?? 0) }}</small></div>
    </div>

    <div v-else-if="report.intent === 'price_compare'" class="market-highlight">
      <div><span>主流挂牌区间</span><strong>{{ formatWanAmount(report.price.p25Wan) }} – {{ formatWanAmount(report.price.p75Wan) }}</strong></div>
      <div><span>中位挂牌价</span><strong>{{ formatWanAmount(report.price.medianWan) }}</strong></div>
      <div><span>可比样本</span><strong>{{ formatNumber(report.reliability.sampleSize) }} 条</strong></div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { Sparkles } from 'lucide-vue-next'
import AgentMarkdownContent from './AgentMarkdownContent.vue'
import ReportReliabilitySummary from './ReportReliabilitySummary.vue'
import type { PriceCompareReport, RecommendationReport } from '../../types/analysis'
import { formatNumber, formatScore, formatWanAmount } from '../../utils/formatters'

defineProps<{ report: RecommendationReport | PriceCompareReport }>()
</script>

<style scoped>
.insight-card {
  padding: clamp(22px, 4vw, 32px);
  border-radius: 16px;
  background: var(--color-teal-soft);
}

.insight-card__topline {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.insight-card__marker {
  display: grid;
  width: 38px;
  height: 38px;
  place-items: center;
  color: var(--color-orange);
  border-radius: 12px;
  background: var(--color-orange-soft);
}

h2 {
  max-width: 680px;
  margin: 0 0 12px;
  color: var(--color-navy);
  font-size: clamp(23px, 3vw, 32px);
  letter-spacing: -0.03em;
  line-height: 1.2;
}

.insight-card__text {
  max-width: 680px;
  margin: 0;
  color: var(--color-ink-soft);
  font-size: 14px;
  line-height: 1.75;
}

.insight-card > :deep(.reliability-summary) {
  border-color: rgb(15 45 70 / 0.1);
}

.metric-grid,
.market-highlight {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 12px;
  margin-top: 28px;
  padding-top: 22px;
  border-top: 1px solid rgb(15 45 70 / 0.1);
}

.metric,
.market-highlight > div {
  display: grid;
  gap: 4px;
}

.metric span,
.market-highlight span {
  color: var(--color-ink-muted);
  font-size: 12px;
}

.metric strong,
.market-highlight strong {
  overflow: hidden;
  color: var(--color-navy);
  font-family: var(--font-mono);
  font-size: clamp(17px, 2.2vw, 24px);
  letter-spacing: -0.055em;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.metric small {
  color: var(--color-ink-muted);
  font-size: 12px;
}

.market-highlight strong {
  font-family: var(--font-sans);
  font-size: clamp(16px, 2vw, 21px);
}

@media (max-width: 600px) {
  .metric-grid,
  .market-highlight {
    grid-template-columns: 1fr;
  }
}
</style>
