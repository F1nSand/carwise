<template>
  <section class="runtime-panel" aria-labelledby="runtime-title">
    <div class="runtime-panel__header">
      <div>
          <h2 id="runtime-title">{{ failed ? 'Agent 本次分析已结束' : 'Agent 正在推进这次分析' }}</h2>
      </div>
      <span class="runtime-status">{{ statusLabel }}</span>
    </div>

    <div class="runtime-panel__scroll">
      <section class="runtime-block" aria-labelledby="plan-title">
        <div class="runtime-block__title"><h3 id="plan-title">当前计划</h3><span>{{ runtime.plan.length ? runtime.plan.length + ' 个步骤' : emptyPlanLabel }}</span></div>
        <ul v-if="runtime.taskGoals?.length" class="goal-list" aria-label="任务交付目标">
          <li v-for="goal in runtime.taskGoals" :key="goal.id"><strong>{{ goal.deliverable }}</strong><small>{{ goal.description }}</small></li>
        </ul>
        <p v-if="runtime.planChangeReason" class="runtime-empty">计划调整：{{ runtime.planChangeReason }}</p>
        <div v-if="runtime.plan.length" class="runtime-progress" role="progressbar" :aria-valuenow="settledPlanCount" :aria-valuemin="0" :aria-valuemax="runtime.plan.length" :aria-label="`已收口 ${settledPlanCount} / ${runtime.plan.length} 步`">
          <span class="runtime-progress__label">已完成/跳过 {{ settledPlanCount }} / {{ runtime.plan.length }} 步</span>
          <span class="runtime-progress__track"><span :style="{ transform: `scaleX(${planProgress})` }"></span></span>
        </div>
        <TransitionGroup v-if="runtime.plan.length" tag="ol" name="plan-list-motion" class="plan-list">
          <li v-for="step in runtime.plan" :key="step.id" :class="`plan-item plan-item--${step.status || 'queued'}`">
            <span class="plan-item__marker" aria-hidden="true"><Check v-if="step.status === 'complete' || step.status === 'skipped'" :size="14" /><LoaderCircle v-else-if="step.status === 'running'" class="runtime-spin" :size="14" /><Circle v-else :size="10" /></span>
            <span><strong>{{ step.title }}</strong><small>{{ step.description }}</small><small v-if="step.reason">{{ step.reason }}</small></span>
            <em>{{ step.status === 'skipped' ? '已跳过' : step.status === 'complete' ? '已完成' : step.status === 'running' ? '进行中' : '待执行' }}</em>
          </li>
        </TransitionGroup>
        <p v-else class="runtime-empty">正在生成计划，稍后会显示完整执行阶段。</p>
      </section>

      <Transition name="state-fade">
      <section v-if="runtime.waitState" class="runtime-wait" aria-live="polite">
        <span class="runtime-wait__dot" aria-hidden="true"></span>
        <span><strong>状态：等待你的回答</strong><small>收到回答后继续当前计划步骤，不会新增计划项。</small></span>
      </section>
      </Transition>

      <section class="runtime-block" aria-labelledby="activity-title">
        <div class="runtime-block__title"><h3 id="activity-title">工具活动</h3><span>{{ runtime.activities.length }} 次</span></div>
        <TransitionGroup v-if="runtime.activities.length" tag="ul" name="activity-list-motion" class="activity-list">
          <li v-for="activity in runtime.activities" :key="activity.id" :class="[`activity activity--${activity.status}`, activity.resolution === 'handled' ? 'activity--handled' : '']">
            <span class="activity__marker" aria-hidden="true"><Check v-if="activity.status === 'complete' || activity.resolution === 'handled'" :size="14" /><LoaderCircle v-else-if="activity.status === 'running'" class="runtime-spin" :size="14" /><Circle v-else :size="10" /></span>
            <span><strong>{{ activity.toolName }}</strong><small>{{ activity.detail }}</small></span>
            <em>{{ activity.status === 'running' ? '运行中' : activity.status === 'waiting' ? '等待回答' : activity.status === 'error' && activity.resolution !== 'handled' ? '需处理' : '已处理' }}</em>
          </li>
        </TransitionGroup>
        <p v-else class="runtime-empty">等待 Agent 调用第一个受控工具。</p>
      </section>
    </div>

    <p class="runtime-panel__note">只展示可验证的计划和工具状态，不展示模型隐藏思考。</p>
  </section>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { Check, Circle, LoaderCircle } from 'lucide-vue-next'
import type { AgentRuntimeState } from '../../types/analysis'

const props = defineProps<{ runtime: AgentRuntimeState; failed?: boolean }>()
const settledPlanCount = computed(() => props.runtime.plan.filter((step) => step.status === 'complete' || step.status === 'skipped').length)
const planProgress = computed(() => props.runtime.plan.length ? settledPlanCount.value / props.runtime.plan.length : 0)
const emptyPlanLabel = computed(() => props.runtime.taskMode === 'simple'
  ? '正在生成计划'
  : props.runtime.taskMode === 'planned'
    ? '计划准备中'
    : '正在理解任务')
const statusLabel = computed(() => props.failed
  ? '未完成'
  : props.runtime.status === 'waiting_user'
  ? '等待你的回答'
  : props.runtime.status === 'done'
    ? '已完成'
    : props.runtime.status === 'blocked'
      ? '计划未完成'
      : '运行中')
</script>

<style scoped>
.runtime-panel { max-width: 900px; margin: 0 auto; padding: clamp(22px, 4vw, 34px); border: 1px solid var(--color-border); border-radius: 16px; background: var(--color-surface); }
.runtime-panel__header, .runtime-block__title { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }
.runtime-panel h2 { margin: 0; color: var(--color-navy); font-size: 23px; letter-spacing: -0.04em; }
.runtime-status { padding: 7px 10px; color: var(--color-teal); border-radius: 999px; background: var(--color-teal-soft); font-size: 12px; font-weight: 750; white-space: nowrap; }
.runtime-panel__scroll { max-height: min(48dvh, 470px); overflow-y: auto; padding-right: 4px; scrollbar-color: var(--color-border-strong) transparent; scrollbar-width: thin; }
.runtime-block { margin-top: 22px; padding-top: 18px; border-top: 1px solid var(--color-border); }
.runtime-block__title h3 { margin: 0; color: var(--color-navy); font-size: 13px; }
.runtime-block__title span { color: var(--color-ink-muted); font-size: 12px; }
.plan-list, .activity-list { display: grid; gap: 9px; margin: 14px 0 0; padding: 0; list-style: none; }
.goal-list { display: grid; gap: 7px; margin: 14px 0 0; padding: 0; list-style: none; }
.goal-list li { display: grid; gap: 3px; padding: 9px 12px; border-left: 2px solid var(--color-teal); background: var(--color-surface-subtle); }
.goal-list strong { color: var(--color-navy); font-size: 12px; }
.goal-list small { color: var(--color-ink-muted); font-size: 12px; }
.plan-item, .activity { display: flex; align-items: center; gap: 11px; min-height: 54px; padding: 9px 12px; border: 1px solid transparent; border-radius: 11px; background: var(--color-surface-subtle); }
.plan-item--running, .activity--running { border-color: rgb(15 45 70 / 0.14); background: #fff; }
.plan-item--complete, .activity--handled { border-color: rgb(15 118 110 / 0.14); background: var(--color-teal-soft); }
.activity--waiting { border-color: rgb(194 65 12 / 0.2); background: var(--color-orange-soft); }
.activity--error:not(.activity--handled) { border-color: rgb(180 35 24 / 0.22); background: var(--color-danger-soft); }
.plan-item__marker, .activity__marker { display: grid; width: 24px; height: 24px; flex: 0 0 24px; place-items: center; color: var(--color-ink-muted); border: 1px solid currentColor; border-radius: 50%; }
.activity__marker { width: 22px; height: 22px; flex-basis: 22px; }
.plan-item--complete .plan-item__marker, .activity--handled .activity__marker, .activity--complete .activity__marker { color: var(--color-teal); }
.plan-item--running .plan-item__marker, .activity--running .activity__marker { color: var(--color-orange); }
.runtime-spin { animation: runtime-spin 900ms linear infinite; }
@keyframes runtime-spin { to { transform: rotate(360deg); } }
.plan-item > span:last-child, .activity > span:nth-child(2) { display: grid; gap: 3px; min-width: 0; }
.plan-item strong, .activity strong { color: var(--color-navy); font-size: 13px; }
.plan-item small, .activity small { color: var(--color-ink-muted); font-size: 12px; line-height: 1.5; }
.plan-item em, .activity em { margin-left: auto; color: var(--color-ink-muted); font-size: 12px; font-style: normal; white-space: nowrap; }
.plan-item--complete em, .activity--handled em { color: var(--color-teal); }
.plan-item--running em, .activity--waiting em { color: var(--color-orange); font-weight: 700; }
.activity--complete em { color: var(--color-teal); }
.activity--error:not(.activity--handled) em { color: var(--color-danger); }
.runtime-empty { margin: 13px 0 0; color: var(--color-ink-muted); font-size: 12px; }
.runtime-progress { display: grid; gap: 8px; margin-top: 15px; }
.runtime-progress__label { color: var(--color-ink-muted); font-size: 12px; }
.runtime-progress__track { height: 5px; overflow: hidden; border-radius: 999px; background: var(--color-surface-subtle); }
.runtime-progress__track > span { display: block; width: 100%; height: 100%; transform-origin: left center; border-radius: inherit; background: var(--color-teal); transition: transform var(--duration-standard) var(--ease-standard); }
.runtime-wait { display: flex; align-items: center; gap: 11px; margin-top: 14px; padding: 12px; border: 1px dashed rgb(194 65 12 / 0.28); border-radius: 11px; background: var(--color-orange-soft); }
.runtime-wait > span:last-child { display: grid; gap: 3px; }
.runtime-wait strong { color: var(--color-orange); font-size: 12px; }
.runtime-wait small { color: var(--color-ink-muted); font-size: 12px; }
.runtime-wait__dot { width: 9px; height: 9px; flex: 0 0 9px; border-radius: 50%; background: var(--color-orange); box-shadow: 0 0 0 4px rgb(194 65 12 / 0.1); }
.runtime-panel__note { display: flex; margin: 23px 0 0; padding-top: 16px; color: var(--color-ink-muted); border-top: 1px solid var(--color-border); font-size: 12px; }
@media (max-width: 760px) { .runtime-panel__scroll { max-height: none; overflow: visible; padding-right: 0; } }
@media (max-width: 520px) { .runtime-panel__header { align-items: flex-start; flex-direction: column; } .runtime-panel__note { font-size: 12px; } .plan-item, .activity { align-items: flex-start; flex-wrap: wrap; } .plan-item em, .activity em { margin-left: 35px; } }
@media (prefers-reduced-motion: reduce) { .runtime-spin { animation: none; } .runtime-progress__track > span { transition: background-color var(--duration-fast) linear; } }
</style>
