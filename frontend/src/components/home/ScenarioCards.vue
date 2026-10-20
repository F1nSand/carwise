<template>
  <section class="scenario-section" aria-labelledby="scenario-title">
    <div class="section-heading">
      <div>
        <h2 id="scenario-title">你想先解决哪一个问题？</h2>
      </div>
      <p>内部分析工具会根据问题自动组合，不需要你先学会选工具。</p>
    </div>

    <div class="scenario-grid">
      <button v-for="scenario in scenarios" :key="scenario.intent" type="button" class="scenario-card" @click="emit('select', scenario.intent)">
        <span class="scenario-card__icon" :class="`scenario-card__icon--${scenario.intent}`" aria-hidden="true"><component :is="scenario.icon" :size="21" :stroke-width="1.8" /></span>
        <span class="scenario-card__content">
          <strong>{{ scenario.title }}</strong>
          <span>{{ scenario.description }}</span>
        </span>
        <ArrowUpRight class="scenario-card__arrow" :size="18" aria-hidden="true" />
      </button>
    </div>
  </section>
</template>

<script setup lang="ts">
import { ArrowUpRight, ChartNoAxesCombined, Sparkles } from 'lucide-vue-next'
import type { AnalysisIntent } from '../../types/analysis'

const emit = defineEmits<{ select: [intent: AnalysisIntent] }>()

const scenarios = [
  {
    intent: 'recommend' as const,
    icon: Sparkles,
    title: '智能购车',
    description: '告诉我预算、车龄和偏好，筛出值得比较的车系。',
  },
  {
    intent: 'price_compare' as const,
    icon: ChartNoAxesCombined,
    title: '车型行情',
    description: '查看同款挂牌区间、年份差异和城市价差。',
  },
]
</script>

<style scoped>
.scenario-section {
  margin-top: var(--space-8);
}

.section-heading {
  display: flex;
  align-items: end;
  justify-content: space-between;
  gap: var(--space-6);
  margin-bottom: 16px;
}

h2 {
  margin: 0;
  color: var(--color-navy);
  font-size: 20px;
  letter-spacing: -0.035em;
}

.section-heading > p {
  max-width: 330px;
  margin: 0;
  color: var(--color-ink-muted);
  font-size: 13px;
  line-height: 1.6;
  text-align: right;
}

.scenario-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 14px;
}

.scenario-card {
  position: relative;
  display: flex;
  align-items: flex-start;
  gap: 16px;
  min-height: 112px;
  padding: 18px;
  overflow: hidden;
  color: var(--color-ink);
  text-align: left;
  border: 1px solid var(--color-border);
  border-radius: 14px;
  background: var(--color-surface);
  transition: border-color var(--duration-fast) var(--ease-standard), background-color var(--duration-fast) var(--ease-standard);
}

@media (hover: hover) and (pointer: fine) {
  .scenario-card:hover {
    border-color: rgb(15 118 110 / 0.32);
    background: rgb(15 118 110 / 0.035);
  }
}

.scenario-card__icon {
  display: grid;
  width: 44px;
  height: 44px;
  flex: 0 0 44px;
  place-items: center;
  border-radius: 14px;
}

.scenario-card__icon--recommend {
  color: var(--color-orange);
  background: var(--color-orange-soft);
}

.scenario-card__icon--price_compare {
  color: var(--color-teal);
  background: var(--color-teal-soft);
}

.scenario-card__content {
  display: grid;
  gap: 7px;
  padding-right: 22px;
}

.scenario-card strong {
  color: var(--color-navy);
  font-size: 17px;
  letter-spacing: -0.02em;
}

.scenario-card__content > span:last-child {
  color: var(--color-ink-soft);
  font-size: 13px;
  line-height: 1.65;
}

.scenario-card__arrow {
  position: absolute;
  z-index: 1;
  right: 20px;
  bottom: 20px;
  color: var(--color-ink-muted);
  transition: color var(--duration-fast) var(--ease-standard), transform var(--duration-fast) var(--ease-standard);
}

@media (hover: hover) and (pointer: fine) {
  .scenario-card:hover .scenario-card__arrow {
    color: var(--color-teal);
    transform: translate(2px, -2px);
  }
}

@media (max-width: 680px) {
  .scenario-grid {
    grid-template-columns: 1fr;
    gap: 8px;
  }

  .section-heading {
    align-items: flex-start;
    flex-direction: column;
    gap: 8px;
  }

  .section-heading > p {
    text-align: left;
  }

  .scenario-card { padding-inline: 0; }
}

@media (prefers-reduced-motion: reduce) {
  .scenario-card { transition: background-color var(--duration-fast) linear; }
  .scenario-card__arrow { transition: color var(--duration-fast) linear; }
  .scenario-card:hover .scenario-card__arrow { transform: none; }
}
</style>
