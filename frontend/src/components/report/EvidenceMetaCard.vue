<template>
  <section class="report-section" aria-labelledby="evidence-title">
    <div class="section-heading"><h2 id="evidence-title">代表性车源查询</h2><span>{{ report.evidence.queryCount }} 次查询</span></div>
    <div class="evidence-query-grid"><article v-for="(query, index) in report.evidence.queries" :key="`${index}-${query.note}`" class="evidence-query"><strong>{{ query.matchCount }} 条命中</strong><span>{{ query.filtersApplied.join(' / ') || '未附加筛选条件' }}</span><small>{{ query.note }}</small></article></div>
  </section>
</template>

<script setup lang="ts">
import type { EvidenceReport } from '../../types/analysis'

defineProps<{ report: EvidenceReport }>()
</script>

<style scoped>
.report-section { display: grid; gap: 16px; }
.section-heading { display: flex; align-items: end; justify-content: space-between; gap: 16px; }
.section-heading h2 { margin: 0; color: var(--color-navy); font-size: 20px; letter-spacing: -0.04em; }
.section-heading > span { color: var(--color-ink-muted); font-size: 12px; }
.evidence-query-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
.evidence-query { display: grid; gap: 5px; min-width: 0; padding: 16px; border: 1px solid var(--color-border); border-radius: 14px; background: var(--color-surface); }
.evidence-query strong { color: var(--color-navy); font-family: var(--font-mono); font-size: 18px; }
.evidence-query span, .evidence-query small { color: var(--color-ink-muted); font-size: 12px; line-height: 1.55; overflow-wrap: anywhere; }
@media (max-width: 650px) { .evidence-query-grid { grid-template-columns: 1fr; } .section-heading { align-items: flex-start; flex-direction: column; } }
</style>
