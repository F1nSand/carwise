<template>
  <section class="conversation-plan" aria-labelledby="conversation-plan-title">
    <button class="conversation-plan__toggle" type="button" :aria-expanded="expanded" @click="emit('update:expanded', !expanded)">
      <span class="conversation-plan__icon"><ListChecks :size="17" aria-hidden="true" /></span>
      <span class="conversation-plan__heading"><strong id="conversation-plan-title">当前计划</strong><small>{{ planLabel }}</small></span>
      <span class="conversation-plan__count">{{ runtime.plan.length ? `${completeCount}/${runtime.plan.length}` : '准备中' }}</span>
      <ChevronDown :size="16" class="conversation-plan__chevron" :class="{ 'conversation-plan__chevron--open': expanded }" aria-hidden="true" />
    </button>
    <Transition name="plan-expand"><ol v-if="expanded" class="conversation-plan__steps">
      <li v-for="step in runtime.plan" :key="step.id" :class="`step step--${step.status}`">
        <Transition name="step-marker" mode="out-in"><span :key="step.status" class="step__marker" aria-hidden="true"><Check v-if="step.status === 'complete'" :size="13" /><LoaderCircle v-else-if="step.status === 'running'" class="spin" :size="13" /><Circle v-else-if="step.status === 'skipped'" :size="11" /><Circle v-else :size="7" /></span></Transition>
        <span class="step__copy"><strong>{{ step.title }}</strong><small v-if="step.description">{{ step.description }}</small></span>
        <small class="step__status">{{ step.status === 'complete' ? '完成' : step.status === 'running' ? '进行中' : step.status === 'skipped' ? '已跳过' : '待执行' }}</small>
      </li>
      <li v-if="runtime.planChangeReason" class="step__reason">计划调整：{{ runtime.planChangeReason }}</li>
    </ol></Transition>
  </section>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { Check, ChevronDown, Circle, ListChecks, LoaderCircle } from 'lucide-vue-next'
import type { AgentRuntimeState } from '../../types/analysis'

const props = withDefaults(defineProps<{ runtime: AgentRuntimeState; expanded?: boolean }>(), { expanded: true })
const emit = defineEmits<{ 'update:expanded': [expanded: boolean] }>()
const expanded = computed(() => props.expanded)
const completeCount = computed(() => props.runtime.plan.filter((step) => step.status === 'complete').length)
const skippedCount = computed(() => props.runtime.plan.filter((step) => step.status === 'skipped').length)
const planLabel = computed(() => props.runtime.status === 'waiting_user'
  ? '等待你的回答后继续'
  : props.runtime.plan.length
    ? `${completeCount.value} 项完成${skippedCount.value ? ` · ${skippedCount.value} 项跳过` : ''}`
    : props.runtime.taskMode === 'planned' ? '计划准备中' : '正在生成计划')
</script>

<style scoped>
.conversation-plan { position: sticky; z-index: 8; top: 0; flex: 0 0 auto; border-bottom: 1px solid var(--color-border); background: rgb(255 255 255 / .94); backdrop-filter: blur(12px); }
.conversation-plan__toggle { display: flex; width: 100%; align-items: center; gap: 10px; min-height: 54px; padding: 8px max(20px, calc((100% - 800px) / 2)); color: var(--color-navy); border: 0; background: transparent; text-align: left; }
.conversation-plan__icon { display: grid; width: 30px; height: 30px; flex: 0 0 30px; place-items: center; color: var(--color-teal); border-radius: 9px; background: var(--color-teal-soft); }
.conversation-plan__heading { display: grid; flex: 1; gap: 2px; }
.conversation-plan__heading strong { font-size: 12px; font-weight: 750; }
.conversation-plan__heading small, .conversation-plan__count { color: var(--color-ink-muted); font-size: 10px; }
.conversation-plan__count { padding: 4px 7px; border: 1px solid var(--color-border); border-radius: 999px; font-variant-numeric: tabular-nums; }
.conversation-plan__chevron { color: var(--color-ink-muted); transition: transform 160ms ease; }
.conversation-plan__chevron--open { transform: rotate(180deg); }
.conversation-plan__steps { display: grid; max-height: min(34dvh, 250px); gap: 3px; margin: 0; padding: 0 max(20px, calc((100% - 800px) / 2)) 12px; overflow: auto; list-style: none; }
.step { display: flex; align-items: center; gap: 9px; min-height: 35px; padding: 5px 7px; border-radius: 8px; }
.step--running { background: #f4f8f8; }
.step__marker { display: grid; width: 20px; height: 20px; flex: 0 0 20px; place-items: center; color: var(--color-ink-muted); }
.step--complete .step__marker { color: var(--color-teal); }
.step--running .step__marker { color: var(--color-orange); }
.step--skipped .step__marker { color: var(--color-ink-muted); }
.step__copy { display: grid; flex: 1; gap: 2px; }
.step__copy strong { color: var(--color-ink); font-size: 11px; font-weight: 650; }
.step__copy small, .step__status, .step__reason { color: var(--color-ink-muted); font-size: 10px; }
.step__reason { padding: 6px 7px; }
.spin { animation: spin 900ms linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
.plan-expand-enter-active, .plan-expand-leave-active { transition: opacity 150ms ease, transform 150ms ease; }
.plan-expand-enter-from, .plan-expand-leave-to { opacity: 0; transform: translateY(-4px); }
.step-marker-enter-active, .step-marker-leave-active { transition: opacity 120ms ease-out, transform 180ms cubic-bezier(.16, 1, .3, 1); }
.step-marker-enter-from, .step-marker-leave-to { opacity: 0; transform: scale(.72); }
@media (max-width: 760px) { .conversation-plan__toggle, .conversation-plan__steps { padding-right: 15px; padding-left: 15px; } }
@media (prefers-reduced-motion: reduce) { .spin { animation: none; } .conversation-plan__chevron, .plan-expand-enter-active, .plan-expand-leave-active, .step-marker-enter-active, .step-marker-leave-active { transition: none; } }
</style>
