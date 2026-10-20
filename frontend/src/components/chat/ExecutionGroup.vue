<template>
  <details class="execution-group" :open="open" @toggle="onToggle">
    <summary>
      <span class="execution-group__indicator" :class="`execution-group__indicator--${status}`"><LoaderCircle v-if="status === 'running'" :size="13" class="spin" /><Check v-else-if="status === 'complete'" :size="13" /><X v-else-if="status === 'error'" :size="13" /><Circle v-else :size="9" /></span>
      <span>{{ title }}</span>
      <small>{{ groupSummary }}</small>
      <ChevronDown :size="15" class="execution-group__chevron" />
    </summary>
    <ol v-if="plan.length" class="execution-group__plan">
      <li v-for="step in plan" :key="step.id" :class="`execution-step execution-step--${step.status}`">
        <span class="execution-step__marker" aria-hidden="true"><Check v-if="step.status === 'complete'" :size="11" /><X v-else-if="step.status === 'skipped'" :size="11" /><Circle v-else :size="7" /></span>
        <span><strong>{{ step.title }}</strong><small>{{ planStateLabel(step.status) }}</small></span>
      </li>
    </ol>
    <ul v-if="activities.length" class="execution-group__items">
      <li v-for="activity in activities" :key="activity.id" :class="`tool-item tool-item--${activity.status}`">
        <span class="tool-item__marker" aria-hidden="true"><Check v-if="activity.status === 'complete' || activity.resolution === 'handled'" :size="12" /><LoaderCircle v-else-if="activity.status === 'running'" :size="12" class="spin" /><X v-else-if="activity.status === 'error'" :size="12" /><Circle v-else :size="8" /></span>
        <span class="tool-item__copy"><strong>{{ activity.toolName }}</strong><small>{{ activity.detail || stateLabel(activity.status) }}</small></span>
        <details v-if="activity.output && Object.keys(activity.output).length" class="tool-item__detail">
          <summary>摘要</summary><pre>{{ safeSummary(activity.output) }}</pre>
        </details>
      </li>
    </ul>
    <p v-else class="execution-group__empty">{{ summary || '正在准备执行步骤。' }}</p>
  </details>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { Check, ChevronDown, Circle, LoaderCircle, X } from 'lucide-vue-next'
import type { AgentPlanStep, AgentToolActivity } from '../../types/analysis'

const props = withDefaults(defineProps<{ title?: string; status?: 'running' | 'complete' | 'error' | 'idle'; activities: AgentToolActivity[]; plan?: AgentPlanStep[]; summary?: string; open?: boolean }>(), {
  title: '执行过程', status: 'running', open: false,
})
const emit = defineEmits<{ 'update:open': [open: boolean] }>()
const plan = computed(() => props.plan || [])
const groupSummary = computed(() => [
  plan.value.length ? `${plan.value.length} 项计划` : '',
  props.activities.length ? `${props.activities.length} 项工具活动` : '',
].filter(Boolean).join(' · ') || 'Agent 执行摘要')

function onToggle(event: Event) {
  emit('update:open', (event.currentTarget as HTMLDetailsElement).open)
}

function stateLabel(status: AgentToolActivity['status']): string {
  return status === 'running' ? '运行中' : status === 'waiting' ? '等待回答' : status === 'error' ? '需要处理' : '已完成'
}

function planStateLabel(status: AgentPlanStep['status']): string {
  return status === 'complete' ? '已完成' : status === 'skipped' ? '已跳过' : status === 'running' ? '进行中' : '未执行'
}

function safeSummary(value: Record<string, unknown>): string {
  const entries = Object.entries(value).slice(0, 8).map(([key, item]) => `${key}: ${typeof item === 'string' ? item.slice(0, 240) : JSON.stringify(item)?.slice(0, 240)}`)
  return entries.join('\n')
}
</script>

<style scoped>
.execution-group { width: fit-content; max-width: min(720px, 92%); margin: 4px 0 4px 40px; color: var(--color-ink-soft); border: 1px solid var(--color-border); border-radius: 10px; background: #fafcfc; font-size: 11px; }
.execution-group > summary { display: flex; align-items: center; gap: 8px; min-height: 37px; padding: 7px 10px; cursor: pointer; list-style: none; }
.execution-group > summary::-webkit-details-marker { display: none; }
.execution-group > summary > small { margin-left: 4px; color: var(--color-ink-muted); font-size: 10px; }
.execution-group__indicator { display: grid; width: 19px; height: 19px; place-items: center; color: var(--color-teal); }
.execution-group__indicator--running { color: var(--color-orange); }
.execution-group__indicator--error, .tool-item--error .tool-item__marker { color: var(--color-danger); }
.execution-group__chevron { margin-left: auto; color: var(--color-ink-muted); transition: transform 150ms ease; }
.execution-group[open] .execution-group__chevron { transform: rotate(180deg); }
.execution-group__items { display: grid; gap: 2px; margin: 0; padding: 0 9px 9px; list-style: none; }
.execution-group__plan { display: grid; gap: 2px; margin: 0; padding: 0 9px 7px; list-style: none; }
.execution-step { display: flex; align-items: center; gap: 8px; padding: 5px 7px; border-radius: 7px; background: #fff; }
.execution-step__marker { display: grid; width: 17px; height: 17px; flex: 0 0 17px; place-items: center; color: var(--color-ink-muted); }
.execution-step--complete .execution-step__marker { color: var(--color-teal); }
.execution-step > span:last-child { display: grid; gap: 2px; }
.execution-step strong { color: var(--color-ink); font-size: 10px; font-weight: 600; }
.execution-step small { color: var(--color-ink-muted); font-size: 9px; }
.tool-item { display: flex; align-items: center; gap: 8px; min-height: 32px; padding: 5px 7px; border-radius: 7px; background: #fff; }
.tool-item__marker { display: grid; width: 17px; height: 17px; flex: 0 0 17px; place-items: center; color: var(--color-ink-muted); }
.tool-item--complete .tool-item__marker { color: var(--color-teal); }
.tool-item--running .tool-item__marker { color: var(--color-orange); }
.tool-item__copy { display: grid; min-width: 130px; gap: 2px; }
.tool-item__copy strong { color: var(--color-ink); font-size: 10px; }
.tool-item__copy small { color: var(--color-ink-muted); font-size: 10px; }
.tool-item__detail { margin-left: auto; }
.tool-item__detail > summary { cursor: pointer; color: var(--color-teal); font-size: 10px; }
.tool-item__detail pre { max-width: 530px; max-height: 220px; overflow: auto; white-space: pre-wrap; color: var(--color-ink-muted); font: 10px/1.5 var(--font-mono); }
.execution-group__empty { margin: 0; padding: 0 12px 10px 37px; color: var(--color-ink-muted); font-size: 10px; }
.spin { animation: spin 900ms linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .spin { animation: none; } .execution-group__chevron { transition: none; } }
</style>
