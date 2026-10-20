<template>
  <aside class="trust-rail" aria-labelledby="trust-title">
    <div class="trust-rail__header">
      <div>
        <h2 id="trust-title">这份结果有多可靠？</h2>
      </div>
      <span class="trust-icon" aria-hidden="true"><ShieldCheck :size="18" /></span>
    </div>

    <div class="reliability" :class="`reliability--${report.reliability.confidence}`">
      <div class="reliability__topline"><span>可靠性等级</span><strong>{{ report.reliability.label }}</strong></div>
      <p>{{ report.reliability.reason }}</p>
      <div class="reliability__sample"><strong>{{ report.reliability.sampleSize === null ? '—' : formatNumber(report.reliability.sampleSize) }}</strong><span>{{ report.reliability.sampleSize === null ? '未形成可比样本' : '条可比样本' }}</span></div>
    </div>

    <section v-if="report.filtersApplied.length" class="rail-section">
      <h3><Filter :size="14" aria-hidden="true" /> 已应用条件</h3>
      <ul class="filter-list"><li v-for="filter in report.filtersApplied" :key="filter">{{ filter }}</li></ul>
    </section>

    <section class="rail-section">
      <h3><Calculator :size="14" aria-hidden="true" /> 统计口径</h3>
      <p class="methodology">{{ report.methodology }}</p>
    </section>

    <section class="rail-section rail-section--warning">
      <h3><TriangleAlert :size="14" aria-hidden="true" /> 数据限制</h3>
      <ul class="limit-list"><li v-for="limitation in report.limitations" :key="limitation">{{ limitation }}</li></ul>
    </section>
  </aside>
</template>

<script setup lang="ts">
import { Calculator, Filter, ShieldCheck, TriangleAlert } from 'lucide-vue-next'
import type { AnalysisReport } from '../../types/analysis'
import { formatNumber } from '../../utils/formatters'

defineProps<{ report: AnalysisReport }>()
</script>

<style scoped>
.trust-rail {
  position: sticky;
  top: 101px;
  align-self: start;
  padding: 22px;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  background: var(--color-surface);
  box-shadow: none;
}

.trust-rail__header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 14px;
}

h2 {
  margin: 0;
  color: var(--color-navy);
  font-size: 18px;
  letter-spacing: -0.04em;
}

.trust-icon {
  display: grid;
  width: 34px;
  height: 34px;
  place-items: center;
  color: var(--color-teal);
  border-radius: 10px;
  background: var(--color-teal-soft);
}

.reliability {
  margin-top: 20px;
  padding: 16px;
  border: 1px solid transparent;
  border-radius: 13px;
}

.reliability--high { color: var(--color-teal); border-color: rgb(15 118 110 / 0.18); background: var(--color-teal-soft); }
.reliability--medium,
.reliability--low { color: var(--color-orange); border-color: rgb(194 65 12 / 0.2); background: var(--color-orange-soft); }
.reliability--none { color: var(--color-danger); border-color: rgb(180 35 24 / 0.2); background: var(--color-danger-soft); }

.reliability__topline {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  font-size: 12px;
}

.reliability__topline strong { font-size: 13px; }
.reliability p { margin: 10px 0 0; color: var(--color-ink-soft); font-size: 13px; line-height: 1.6; }

.reliability__sample {
  display: flex;
  align-items: baseline;
  gap: 7px;
  margin-top: 15px;
  padding-top: 13px;
  border-top: 1px solid rgb(15 118 110 / 0.15);
}

.reliability__sample strong { color: var(--color-navy); font-family: var(--font-mono); font-size: 22px; letter-spacing: -0.06em; }
.reliability__sample span { color: var(--color-ink-muted); font-size: 12px; }

.rail-section { margin-top: 22px; padding-top: 19px; border-top: 1px solid var(--color-border); }
.rail-section h3 { display: flex; align-items: center; gap: 7px; margin: 0; color: var(--color-navy); font-size: 13px; }
.rail-section h3 svg { color: var(--color-teal); }
.filter-list, .limit-list { display: grid; gap: 8px; margin: 12px 0 0; padding-left: 17px; color: var(--color-ink-soft); font-size: 13px; line-height: 1.6; }
.methodology { margin: 12px 0 0; color: var(--color-ink-soft); font-size: 13px; line-height: 1.65; }
.rail-section--warning h3 svg { color: var(--color-orange); }
.limit-list { color: var(--color-ink-muted); }

@media (max-width: 980px) {
  .trust-rail { position: static; padding: 0; border: 0; background: transparent; }
  .trust-rail__header,
  .reliability { display: none; }
}
</style>
