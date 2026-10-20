<template>
  <section class="agent-answer-card" aria-labelledby="agent-answer-title">
    <div class="agent-answer-card__header">
      <div>
        <h2 id="agent-answer-title">{{ report.summaryTitle }}</h2>
      </div>
      <span class="agent-answer-card__badge">Agent</span>
    </div>
    <p class="agent-answer-card__question">“{{ report.question }}”</p>

    <AgentMarkdownContent v-if="hasAnswer" class="agent-answer-card__body" :source="report.answer || report.summaryText" />
    <p v-else class="agent-answer-card__empty">暂无可展示的回答内容</p>
    <ReportReliabilitySummary :report="report" />
    <p class="agent-answer-card__boundary">{{ report.intent === 'evidence' ? '本卡片展示查询命中数和代表性样本，不等同于总体价格统计。' : report.intent === 'no_data' ? '当前没有形成可比样本；可根据下方限制尝试放宽条件。' : '本次为 Agent 直接回答，未生成结构化统计图表。' }}</p>
  </section>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import AgentMarkdownContent from './AgentMarkdownContent.vue'
import ReportReliabilitySummary from './ReportReliabilitySummary.vue'
import type { AgentAnswerReport, EvidenceReport, NoDataReport } from '../../types/analysis'

const props = defineProps<{ report: AgentAnswerReport | EvidenceReport | NoDataReport }>()
const hasAnswer = computed(() => Boolean(props.report.answer || props.report.summaryText))
</script>

<style scoped>
.agent-answer-card { min-width: 0; padding: clamp(22px, 4vw, 32px); border-radius: 16px; background: var(--color-teal-soft); }
.agent-answer-card__header { display: flex; align-items: flex-start; justify-content: space-between; gap: 14px; }
.agent-answer-card h2 { margin: 0; color: var(--color-navy); font-size: clamp(22px, 3vw, 30px); letter-spacing: -0.05em; }
.agent-answer-card__badge { padding: 7px 10px; color: var(--color-teal); border: 1px solid rgb(15 118 110 / 0.18); border-radius: 999px; background: #fff; font-family: var(--font-mono); font-size: 12px; font-weight: 700; letter-spacing: 0.02em; }
.agent-answer-card__question { margin: 10px 0 0; color: var(--color-ink-muted); font-size: 14px; }
.agent-answer-card__body { margin-top: 26px; font-size: 14px; }
.agent-answer-card > :deep(.reliability-summary) { margin-top: 22px; }
.agent-answer-card__empty { margin: 26px 0 0; color: var(--color-ink-muted); font-size: 13px; }
.agent-answer-card__boundary { margin: 24px 0 0; padding-top: 15px; color: var(--color-ink-muted); border-top: 1px solid rgb(15 45 70 / 0.1); font-size: 13px; line-height: 1.6; }
</style>
