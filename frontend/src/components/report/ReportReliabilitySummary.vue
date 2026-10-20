<template>
  <aside class="reliability-summary" :class="`reliability-summary--${report.reliability.confidence}`" aria-label="可靠性摘要">
    <div class="reliability-summary__item">
      <span>可靠性</span>
      <strong>{{ report.reliability.label }}</strong>
    </div>
    <div class="reliability-summary__item">
      <span>可比样本</span>
      <strong>{{ report.reliability.sampleSize === null ? '暂无' : formatNumber(report.reliability.sampleSize) }}</strong>
    </div>
    <p>{{ report.reliability.reason }}</p>
  </aside>
</template>

<script setup lang="ts">
import type { AnalysisReport } from '../../types/analysis'
import { formatNumber } from '../../utils/formatters'

defineProps<{ report: AnalysisReport }>()
</script>

<style scoped>
.reliability-summary {
  display: none;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px 16px;
  padding: 14px 0;
  border-top: 1px solid var(--color-border);
  border-bottom: 1px solid var(--color-border);
}

.reliability-summary__item { display: grid; gap: 3px; }
.reliability-summary__item span { color: var(--color-ink-muted); font-size: 12px; }
.reliability-summary__item strong { color: var(--color-navy); font-size: 14px; }
.reliability-summary--high .reliability-summary__item:first-child strong { color: var(--color-teal); }
.reliability-summary--medium .reliability-summary__item:first-child strong,
.reliability-summary--low .reliability-summary__item:first-child strong { color: var(--color-orange); }
.reliability-summary--none .reliability-summary__item:first-child strong { color: var(--color-danger); }
.reliability-summary p { grid-column: 1 / -1; margin: 0; color: var(--color-ink-soft); font-size: 13px; line-height: 1.6; }

@media (max-width: 980px) {
  .reliability-summary { display: grid; }
}
</style>
