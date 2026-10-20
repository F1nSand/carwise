<template>
  <div v-if="records.length" class="history-list">
    <article v-for="record in records" :key="record.id" class="history-item">
      <div class="history-item__marker" :class="`history-item__marker--${record.intent}`" aria-hidden="true"><Sparkles v-if="record.intent === 'recommend'" :size="17" /><ChartNoAxesCombined v-else :size="17" /></div>
      <div class="history-item__copy">
        <div class="history-item__meta"><span>{{ record.scenarioLabel }}</span><time>{{ formatDateTime(record.createdAt) }}</time></div>
        <h2>{{ record.question }}</h2>
        <p>{{ record.summary }}</p>
      </div>
      <div class="history-item__side">
        <span class="confidence" :class="`confidence--${record.confidence}`">{{ confidenceLabel(record.confidence) }}</span>
        <button v-if="record.status === 'error' && record.recoverable" type="button" class="open-button" @click="emit('recover', record.id)">恢复已保存进度 <ArrowUpRight :size="15" aria-hidden="true" /></button>
        <small v-if="record.status === 'error' && record.recoveryReason">{{ record.recoveryReason }}</small>
        <button v-if="record.status === 'error' && record.retryable" type="button" class="open-button" @click="emit('retry', record.id)">从头重试 <ArrowUpRight :size="15" aria-hidden="true" /></button>
        <button v-else-if="record.status !== 'error'" type="button" class="open-button" @click="emit('open', record.reportId)">查看报告 <ArrowUpRight :size="15" aria-hidden="true" /></button>
      </div>
    </article>
  </div>
  <div v-else class="empty-state">
    <div class="empty-state__icon" aria-hidden="true"><FileClock :size="22" /></div>
    <h2>还没有分析记录</h2>
    <p>完成一次预算推荐或车型行情分析后，报告会出现在这里。</p>
    <RouterLink class="button button--primary" to="/">开始一次分析 <ArrowUpRight :size="16" aria-hidden="true" /></RouterLink>
  </div>
</template>

<script setup lang="ts">
import { ArrowUpRight, ChartNoAxesCombined, FileClock, Sparkles } from 'lucide-vue-next'
import type { AnalysisRecord, Confidence } from '../../types/analysis'
import { formatDateTime } from '../../utils/formatters'

defineProps<{ records: AnalysisRecord[] }>()
const emit = defineEmits<{ open: [reportId: string]; retry: [recordId: string]; recover: [recordId: string] }>()

function confidenceLabel(confidence: Confidence): string {
  return { high: '较高可靠性', medium: '中等可靠性', low: '谨慎参考', none: '暂无结论' }[confidence]
}
</script>

<style scoped>
.history-list { display: grid; gap: 12px; }
.history-item { display: grid; grid-template-columns: 44px minmax(0, 1fr) auto; align-items: start; gap: 16px; padding: 20px; border-bottom: 1px solid var(--color-border); background: transparent; transition: background-color var(--duration-standard) var(--ease-standard); }
.history-item:first-child { border-top: 1px solid var(--color-border); }
@media (hover: hover) and (pointer: fine) { .history-item:hover { background: rgb(15 118 110 / 0.035); } }
.history-item__marker { display: grid; width: 44px; height: 44px; place-items: center; border-radius: 13px; }
.history-item__marker--recommend { color: var(--color-orange); background: var(--color-orange-soft); }
.history-item__marker--price_compare { color: var(--color-teal); background: var(--color-teal-soft); }
.history-item__copy { min-width: 0; }
.history-item__meta { display: flex; align-items: center; gap: 9px; color: var(--color-ink-muted); font-size: 12px; }
.history-item__meta span { color: var(--color-teal); font-weight: 700; }
.history-item__meta time { font-family: var(--font-mono); }
.history-item h2 { margin: 9px 0 5px; color: var(--color-navy); font-size: 16px; letter-spacing: -0.025em; line-height: 1.45; overflow-wrap: anywhere; }
.history-item p { margin: 0; color: var(--color-ink-muted); font-size: 13px; line-height: 1.65; overflow-wrap: anywhere; }
.history-item__side { display: grid; justify-items: end; gap: 20px; }
.confidence { min-height: 32px; padding: 7px 10px; border-radius: 999px; font-size: 12px; font-weight: 700; white-space: nowrap; }
.confidence--high { color: var(--color-teal); background: var(--color-teal-soft); }
.confidence--medium, .confidence--low { color: var(--color-orange); background: var(--color-orange-soft); }
.confidence--none { color: var(--color-danger); background: var(--color-danger-soft); }
.open-button { display: inline-flex; align-items: center; gap: 5px; min-height: 44px; padding: 0 12px; color: var(--color-navy); border: 1px solid var(--color-border); border-radius: 9px; background: #fff; font-size: 13px; font-weight: 700; }
@media (hover: hover) and (pointer: fine) { .open-button:hover { color: var(--color-teal); border-color: var(--color-teal); } }
.empty-state { display: grid; min-height: 300px; place-items: center; align-content: center; gap: 10px; padding: 30px; text-align: center; border: 1px dashed var(--color-border-strong); border-radius: 16px; background: rgb(255 255 255 / 0.6); }
.empty-state__icon { display: grid; width: 50px; height: 50px; place-items: center; color: var(--color-teal); border-radius: 15px; background: var(--color-teal-soft); }
.empty-state h2 { margin: 6px 0 0; color: var(--color-navy); font-size: 21px; letter-spacing: -0.04em; }
.empty-state p { max-width: 320px; margin: 0; color: var(--color-ink-muted); font-size: 13px; line-height: 1.6; }
.button { display: inline-flex; align-items: center; justify-content: center; gap: 7px; min-height: 44px; padding: 0 15px; border-radius: 10px; font-size: 13px; font-weight: 750; }
.button--primary { color: #fff; background: var(--color-navy); }

@media (max-width: 700px) {
  .history-item { grid-template-columns: 44px minmax(0, 1fr); }
  .history-item__side { grid-column: 1 / -1; justify-items: start; display: flex; align-items: flex-start; flex-direction: column; gap: 10px; }
  .history-item__side small { max-width: 100%; overflow-wrap: anywhere; }
}

@media (prefers-reduced-motion: reduce) { .history-item { transition: background-color var(--duration-fast) linear; } }
</style>
