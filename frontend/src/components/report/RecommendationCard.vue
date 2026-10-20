<template>
  <article class="recommendation-card">
    <div class="recommendation-card__topline">
      <span class="rank">0{{ rank }}</span>
      <span class="score"><Gauge :size="14" aria-hidden="true" /> {{ formatScore(recommendation.score) }}</span>
    </div>
    <div class="recommendation-card__title-row">
      <div>
        <p>{{ recommendation.brand }}</p>
        <h3>{{ recommendation.series }}</h3>
      </div>
      <span class="recommendation-card__price">{{ formatWanAmount(recommendation.medianWan) }}<small>中位挂牌价</small></span>
    </div>
    <div class="recommendation-card__stats">
      <span><strong>{{ formatWanAmount(recommendation.p25Wan) }}–{{ formatWanAmount(recommendation.p75Wan) }}</strong>主流区间</span>
      <span><strong>{{ formatNumber(recommendation.sampleSize) }}</strong>样本量</span>
      <span><strong>{{ recommendation.medianYear }}</strong>中位年份</span>
      <span><strong>{{ recommendation.medianMileageWanKm }} 万公里</strong>中位里程</span>
    </div>
    <p class="recommendation-card__reason"><CheckCircle2 :size="15" aria-hidden="true" /> {{ recommendation.reason }}</p>
    <p class="recommendation-card__risk"><TriangleAlert :size="15" aria-hidden="true" /> {{ recommendation.risk }}</p>
  </article>
</template>

<script setup lang="ts">
import { CheckCircle2, Gauge, TriangleAlert } from 'lucide-vue-next'
import type { Recommendation } from '../../types/analysis'
import { formatNumber, formatScore, formatWanAmount } from '../../utils/formatters'

defineProps<{ recommendation: Recommendation; rank: number }>()
</script>

<style scoped>
.recommendation-card {
  padding: 22px;
  border: 1px solid var(--color-border);
  border-radius: 16px;
  background: var(--color-surface);
  transition: border-color var(--duration-standard) var(--ease-standard), background-color var(--duration-standard) var(--ease-standard);
}

@media (hover: hover) and (pointer: fine) {
  .recommendation-card:hover {
    border-color: var(--color-border-strong);
    background: #fff;
  }
}

.recommendation-card__topline,
.recommendation-card__title-row {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 14px;
}

.rank {
  color: var(--color-orange);
  font-family: var(--font-mono);
  font-size: 12px;
  font-weight: 700;
}

.score {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  color: var(--color-teal);
  font-family: var(--font-mono);
  font-size: 12px;
  font-weight: 700;
}

.recommendation-card__title-row {
  align-items: end;
  margin-top: 19px;
}

.recommendation-card__title-row p {
  margin: 0 0 4px;
  color: var(--color-ink-muted);
  font-size: 12px;
}

h3 {
  margin: 0;
  color: var(--color-navy);
  font-size: 22px;
  letter-spacing: -0.04em;
}

.recommendation-card__price {
  display: grid;
  color: var(--color-navy);
  font-family: var(--font-mono);
  font-size: 22px;
  font-weight: 700;
  letter-spacing: -0.06em;
  text-align: right;
  white-space: nowrap;
}

.recommendation-card__price small {
  color: var(--color-ink-muted);
  font-family: var(--font-sans);
  font-size: 12px;
  font-weight: 400;
  letter-spacing: 0;
}

.recommendation-card__stats {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
  margin-top: 22px;
  padding: 14px 0;
  border-top: 1px solid var(--color-border);
  border-bottom: 1px solid var(--color-border);
}

.recommendation-card__stats span {
  display: grid;
  gap: 3px;
  color: var(--color-ink-muted);
  font-size: 12px;
}

.recommendation-card__stats strong {
  color: var(--color-ink-soft);
  font-family: var(--font-mono);
  font-size: 13px;
}

.recommendation-card__reason,
.recommendation-card__risk {
  display: flex;
  align-items: flex-start;
  gap: 7px;
  margin: 15px 0 0;
  color: var(--color-ink-soft);
  font-size: 12px;
  line-height: 1.6;
}

.recommendation-card__reason svg { flex: 0 0 auto; color: var(--color-teal); margin-top: 2px; }
.recommendation-card__risk svg { flex: 0 0 auto; color: var(--color-orange); margin-top: 2px; }

@media (max-width: 430px) {
  .recommendation-card__title-row { align-items: flex-start; flex-direction: column; }
  .recommendation-card__price { text-align: left; }
}

@media (prefers-reduced-motion: reduce) { .recommendation-card { transition: border-color var(--duration-fast) linear, background-color var(--duration-fast) linear; } }
</style>
