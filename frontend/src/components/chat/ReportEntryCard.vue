<template>
  <button class="report-entry" type="button" @click="emit('open', report.reportId)">
    <span class="report-entry__icon"><FileChartColumn :size="18" aria-hidden="true" /></span>
    <span class="report-entry__content"><span class="report-entry__eyebrow">分析报告 <b>v{{ report.version }}</b><time v-if="report.generatedAt">{{ formatDate(report.generatedAt) }}</time></span><strong>{{ report.title || '查看完整分析' }}</strong><small v-if="report.summary">{{ report.summary }}</small></span>
    <span class="report-entry__action">打开报告 <ArrowUpRight :size="15" aria-hidden="true" /></span>
  </button>
</template>

<script setup lang="ts">
import { ArrowUpRight, FileChartColumn } from 'lucide-vue-next'
import type { ConversationReportRef } from '../../types/conversation'

defineProps<{ report: ConversationReportRef }>()
const emit = defineEmits<{ open: [reportId: string] }>()
function formatDate(value: string): string {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}
</script>

<style scoped>
.report-entry { display: flex; width: min(600px, calc(100% - 76px)); align-items: center; gap: 13px; margin: 9px 0 17px 40px; padding: 14px; color: var(--color-ink); border: 1px solid rgb(15 118 110 / .18); border-radius: 13px; background: linear-gradient(115deg, #fff 0%, #f1f8f7 100%); box-shadow: 0 5px 18px rgb(15 45 70 / .045); text-align: left; transition: border-color 150ms ease, box-shadow 150ms ease, transform 150ms ease; }
.report-entry:hover { border-color: rgb(15 118 110 / .45); box-shadow: 0 9px 25px rgb(15 45 70 / .09); transform: translateY(-1px); }
.report-entry__icon { display: grid; width: 39px; height: 39px; flex: 0 0 39px; place-items: center; color: var(--color-teal); border-radius: 11px; background: var(--color-teal-soft); }
.report-entry__content { display: grid; min-width: 0; flex: 1; gap: 4px; }
.report-entry__eyebrow { display: flex; align-items: center; gap: 7px; color: var(--color-ink-muted); font-size: 10px; }
.report-entry__eyebrow b { padding: 2px 5px; color: var(--color-teal); border-radius: 5px; background: #e0f2ef; font-weight: 700; }
.report-entry__eyebrow time { margin-left: auto; }
.report-entry__content > strong { overflow: hidden; color: var(--color-navy); font-size: 13px; text-overflow: ellipsis; white-space: nowrap; }
.report-entry__content > small { overflow: hidden; color: var(--color-ink-muted); font-size: 10px; text-overflow: ellipsis; white-space: nowrap; }
.report-entry__action { display: inline-flex; align-items: center; gap: 5px; color: var(--color-teal); font-size: 10px; font-weight: 700; white-space: nowrap; }
@media (max-width: 560px) { .report-entry { width: calc(100% - 46px); gap: 9px; margin-left: 36px; padding: 11px; } .report-entry__action { font-size: 0; } }
@media (prefers-reduced-motion: reduce) { .report-entry { transition: none; } }
</style>
