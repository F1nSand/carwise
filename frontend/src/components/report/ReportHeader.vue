<template>
  <header class="report-header">
    <div class="report-header__topline">
      <RouterLink class="back-link" :to="conversationTarget"><ArrowLeft :size="15" aria-hidden="true" /> {{ report.conversationId ? '返回会话' : '返回聊天会话' }}</RouterLink>
      <span class="report-header__time">分析于 {{ formatDateTime(report.generatedAt) }}</span>
    </div>
    <div class="report-header__title-row">
      <div>
        <h1>{{ report.scenarioLabel }}报告</h1>
        <p class="report-header__question">“{{ report.question }}”</p>
      </div>
      <span class="data-badge"><Database :size="14" aria-hidden="true" /> {{ report.intent === 'unknown' ? 'Agent 直接回答' : report.intent === 'evidence' ? '样本证据口径' : report.intent === 'no_data' ? '数据边界口径' : '挂牌数据口径' }}</span>
    </div>
  </header>
</template>

<script setup lang="ts">
import { ArrowLeft, Database } from 'lucide-vue-next'
import { computed } from 'vue'
import type { RouteLocationRaw } from 'vue-router'
import type { AnalysisReport } from '../../types/analysis'
import { formatDateTime } from '../../utils/formatters'

const props = defineProps<{ report: AnalysisReport }>()
const conversationTarget = computed<RouteLocationRaw>(() => props.report.conversationId
  ? { name: 'conversation', params: { id: props.report.conversationId } }
  : '/chat')
</script>

<style scoped>
.report-header {
  padding-bottom: 26px;
  border-bottom: 1px solid var(--color-border);
}

.report-header__topline,
.report-header__title-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 18px;
}

.back-link {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  min-height: 40px;
  color: var(--color-ink-soft);
  font-size: 13px;
  font-weight: 650;
  transition: color var(--duration-fast) var(--ease-standard);
}

@media (hover: hover) and (pointer: fine) { .back-link:hover { color: var(--color-teal); } }

.report-header__time {
  color: var(--color-ink-muted);
  font-family: var(--font-mono);
  font-size: 12px;
}

.report-header__title-row {
  align-items: flex-end;
  margin-top: 22px;
}

h1 {
  margin: 0;
  color: var(--color-navy);
  font-size: clamp(28px, 4vw, 42px);
  letter-spacing: -0.065em;
}

.report-header__question {
  margin: 10px 0 0;
  color: var(--color-ink-soft);
  font-size: 15px;
}

.data-badge {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  min-height: 44px;
  padding: 0 11px;
  color: var(--color-teal);
  border: 1px solid rgb(15 118 110 / 0.16);
  border-radius: 999px;
  background: var(--color-teal-soft);
  font-size: 12px;
  font-weight: 700;
  white-space: nowrap;
}

@media (max-width: 620px) {
  .report-header__topline,
  .report-header__title-row {
    align-items: flex-start;
    flex-direction: column;
  }

  .report-header__time { align-self: flex-start; }
}
</style>
