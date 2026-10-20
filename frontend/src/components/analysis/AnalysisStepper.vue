<template>
  <section class="stepper" aria-labelledby="stepper-title" aria-live="polite">
    <div class="stepper__heading">
      <div>
        <h2 id="stepper-title">{{ title }}</h2>
      </div>
      <span class="stepper__count">已完成 {{ completedCount }} / {{ events.length }} 步</span>
    </div>
    <div class="stepper__progress" role="progressbar" :aria-valuenow="completedCount" :aria-valuemin="0" :aria-valuemax="events.length" :aria-label="`已完成 ${completedCount} / ${events.length} 步`">
      <span :style="{ transform: `scaleX(${progress})` }"></span>
    </div>
    <ol class="stepper__list">
      <li v-for="(event, index) in events" :key="event.phase" class="stepper__item" :class="itemClass(index)">
        <span class="stepper__marker" aria-hidden="true"><Check v-if="event.status === 'complete'" :size="15" /><LoaderCircle v-else-if="event.status === 'running'" class="stepper__running" :size="15" /><Circle v-else :size="11" /></span>
        <span class="stepper__copy"><strong>{{ event.label }}</strong><small>{{ event.detail }}</small></span>
        <span class="stepper__status">{{ event.status === 'complete' ? '已完成' : event.status === 'running' ? '进行中' : '排队中' }}</span>
      </li>
    </ol>
  </section>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { Check, Circle, LoaderCircle } from 'lucide-vue-next'
import type { AnalysisProgressEvent } from '../../types/analysis'

const props = withDefaults(
  defineProps<{
    events: AnalysisProgressEvent[]
    title?: string
  }>(),
  { title: '正在整理一份有依据的答案' },
)

defineEmits<{ continue: [] }>()

const completedCount = computed(() => props.events.filter((event) => event.status === 'complete').length)
const progress = computed(() => props.events.length ? completedCount.value / props.events.length : 0)

function itemClass(index: number) {
  return {
    'stepper__item--done': props.events[index]?.status === 'complete',
    'stepper__item--active': props.events[index]?.status === 'running',
  }
}
</script>

<style scoped>
.stepper {
  padding: 28px;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  background: var(--color-surface);
  box-shadow: none;
}

.stepper__heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}

h2 {
  margin: 0;
  color: var(--color-navy);
  font-size: 22px;
  letter-spacing: -0.04em;
}

.stepper__count {
  color: var(--color-teal);
  font-family: var(--font-mono);
  font-size: 12px;
  font-weight: 700;
}

.stepper__progress { height: 5px; margin-top: 20px; overflow: hidden; border-radius: 999px; background: var(--color-surface-subtle); }
.stepper__progress > span { display: block; width: 100%; height: 100%; transform-origin: left center; border-radius: inherit; background: var(--color-teal); transition: transform var(--duration-standard) var(--ease-standard); }

.stepper__list {
  display: grid;
  gap: 10px;
  margin: 28px 0 0;
  padding: 0;
  list-style: none;
}

.stepper__item {
  display: flex;
  align-items: center;
  gap: 13px;
  min-height: 60px;
  padding: 9px 12px;
  border: 1px solid transparent;
  border-radius: 12px;
  background: var(--color-surface-subtle);
}

.stepper__item--done {
  border-color: rgb(15 118 110 / 0.16);
  background: var(--color-teal-soft);
}

.stepper__item--active {
  border-color: rgb(15 45 70 / 0.16);
  background: #fff;
  box-shadow: none;
}

.stepper__marker {
  display: grid;
  width: 29px;
  height: 29px;
  flex: 0 0 29px;
  place-items: center;
  color: var(--color-ink-muted);
  border: 1px solid var(--color-border-strong);
  border-radius: 50%;
  background: #fff;
}

.stepper__item--done .stepper__marker {
  color: #fff;
  border-color: var(--color-teal);
  background: var(--color-teal);
}

.stepper__item--active .stepper__marker {
  color: #fff;
  border-color: var(--color-navy);
  background: var(--color-navy);
}
.stepper__running { animation: stepper-spin 1s linear infinite; }
@keyframes stepper-spin { to { transform: rotate(360deg); } }

.stepper__copy {
  display: grid;
  gap: 3px;
  min-width: 0;
}

.stepper__copy strong {
  color: var(--color-navy);
  font-size: 13px;
}

.stepper__copy small {
  color: var(--color-ink-muted);
  font-size: 12px;
  line-height: 1.5;
  overflow-wrap: anywhere;
}

.stepper__status {
  margin-left: auto;
  color: var(--color-ink-muted);
  font-size: 12px;
  white-space: nowrap;
}

.stepper__item--done .stepper__status { color: var(--color-teal); }
.stepper__item--active .stepper__status { color: var(--color-orange); font-weight: 700; }

@media (max-width: 560px) {
  .stepper { padding: 20px; }
  .stepper__status { display: none; }
}

@media (prefers-reduced-motion: reduce) {
  .stepper__running { animation: none; }
  .stepper__progress > span { transition: background-color var(--duration-fast) linear; }
}
</style>
