<template>
  <section class="review-card" aria-labelledby="review-title">
    <div class="review-card__header">
      <div>
        <h2 id="review-title">{{ draft.title }}</h2>
        <p class="review-card__question">“{{ draft.question }}”</p>
        <p v-if="draft.filters.bodyType && bodyTypeUnsupported" class="review-card__capability-note" role="status">
          当前数据能力声明不支持车型类别筛选。确认后，Agent 会先询问是否放宽“{{ draft.filters.bodyType }}”条件，不会自动移除。
        </p>
      </div>
      <span class="review-card__badge"><Check :size="14" aria-hidden="true" /> 已识别</span>
    </div>

    <Transition name="state-fade" mode="out-in">
    <div v-if="draft.needsClarification" key="clarification" class="clarification" role="group" aria-labelledby="clarification-title">
      <div class="clarification__icon" aria-hidden="true"><CircleHelp :size="20" /></div>
      <div class="clarification__content">
        <h3 id="clarification-title">还差一个关键条件</h3>
        <p>{{ draft.clarification?.question }}</p>
        <div class="clarification__options">
          <button v-for="option in draft.clarification?.options" :key="option" type="button" class="option-button" @click="emit('chooseClarification', option)">
            {{ option }}
          </button>
        </div>
      </div>
    </div>

    <div v-else key="filters">
      <div class="filter-list" aria-label="已识别条件">
        <span v-for="filter in filterItems" :key="filter.label" class="filter-chip">
          <span>{{ filter.label }}</span>
          <strong>{{ filter.value }}</strong>
        </span>
      </div>
      <div class="review-card__actions">
        <button type="button" class="button button--quiet" @click="emit('edit')">返回修改</button>
        <button type="button" class="button button--primary" @click="emit('confirm', draft.filters)">
          确认条件并分析 <ArrowUpRight :size="17" aria-hidden="true" />
        </button>
      </div>
    </div>
    </Transition>
  </section>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { ArrowUpRight, Check, CircleHelp } from 'lucide-vue-next'
import type { AnalysisDraft } from '../../types/analysis'
import type { DataCapabilities } from '../../services/analysisAdapter'

const props = defineProps<{ draft: AnalysisDraft; capabilities?: DataCapabilities | null }>()
const bodyTypeUnsupported = computed(() => !props.capabilities || props.capabilities.unsupported_filters.includes('body_type'))

const emit = defineEmits<{
  confirm: [filters: AnalysisDraft['filters']]
  edit: []
  chooseClarification: [value: string]
}>()

const filterItems = computed(() => {
  const filters = props.draft.filters
  const items: Array<{ label: string; value: string }> = []
  if (filters.budgetMinWan && filters.budgetMaxWan) items.push({ label: '预算', value: `${filters.budgetMinWan}–${filters.budgetMaxWan} 万` })
  else if (filters.budgetMaxWan) items.push({ label: '预算上限', value: `${filters.budgetMaxWan} 万` })
  if (filters.bodyType) items.push({ label: '车型类别', value: filters.bodyType })
  if (filters.series) items.push({ label: '车系', value: filters.series })
  if (filters.year) items.push({ label: '生产年份', value: `${filters.year} 年` })
  if (filters.maxAge) items.push({ label: '车龄', value: `${filters.maxAge} 年内` })
  if (filters.city) items.push({ label: '城市', value: filters.city })
  return items
})
</script>

<style scoped>
.review-card {
  padding: clamp(20px, 4vw, 30px);
  border: 1px solid rgb(15 118 110 / 0.2);
  border-radius: 16px;
  background: linear-gradient(135deg, #fff 0%, #f7fcfb 100%);
  box-shadow: none;
}

.review-card__header,
.review-card__actions {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}

h2 {
  margin: 0;
  color: var(--color-navy);
  font-size: 22px;
  letter-spacing: -0.035em;
}

.review-card__question {
  margin: 10px 0 0;
  color: var(--color-ink-muted);
  font-size: 13px;
}

.review-card__capability-note {
  margin: 10px 0 0;
  color: #9a3412;
  font-size: 13px;
  line-height: 1.6;
}

.review-card__badge {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  min-height: 30px;
  padding: 0 10px;
  color: var(--color-teal);
  border-radius: 999px;
  background: var(--color-teal-soft);
  font-size: 12px;
  font-weight: 700;
  white-space: nowrap;
}

.filter-list {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  margin-top: 26px;
}

.filter-chip {
  display: grid;
  gap: 3px;
  min-width: 110px;
  padding: 11px 13px;
  border: 1px solid var(--color-border);
  border-radius: 12px;
  background: #fff;
}

.filter-chip span {
  color: var(--color-ink-muted);
  font-size: 12px;
}

.filter-chip strong {
  color: var(--color-navy);
  font-size: 14px;
}

.review-card__actions {
  align-items: center;
  justify-content: flex-end;
  margin-top: 26px;
}

.button {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  min-height: 44px;
  padding: 0 16px;
  border: 1px solid transparent;
  border-radius: 11px;
  font-size: 13px;
  font-weight: 750;
  transition: transform var(--duration-fast) var(--ease-standard), background var(--duration-fast) var(--ease-standard);
}

@media (hover: hover) and (pointer: fine) {
  .button:hover {
    transform: translateY(-1px);
  }
}

.button--primary {
  color: #fff;
  background: var(--color-navy);
}

.button--quiet {
  color: var(--color-ink-soft);
  border-color: var(--color-border);
  background: #fff;
}

.button:focus-visible,
.option-button:focus-visible { outline: 2px solid var(--color-teal); outline-offset: 3px; }

.clarification {
  display: flex;
  gap: 14px;
  margin-top: 24px;
  padding: 17px;
  border: 1px solid rgb(194 65 12 / 0.2);
  border-radius: 14px;
  background: var(--color-orange-soft);
}

.clarification__icon {
  display: grid;
  width: 38px;
  height: 38px;
  flex: 0 0 38px;
  place-items: center;
  color: var(--color-orange);
  border-radius: 11px;
  background: #fff;
}

.clarification__content h3 {
  margin: 0;
  color: var(--color-navy);
  font-size: 15px;
}

.clarification__content p {
  margin: 5px 0 13px;
  color: var(--color-ink-soft);
  font-size: 13px;
  line-height: 1.6;
}

.clarification__options {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.option-button {
  min-height: 44px;
  padding: 0 13px;
  color: var(--color-orange);
  border: 1px solid rgb(194 65 12 / 0.25);
  border-radius: 9px;
  background: #fff;
  font-size: 13px;
  font-weight: 700;
}

@media (hover: hover) and (pointer: fine) {
  .option-button:hover {
    border-color: var(--color-orange);
    background: #fffaf6;
  }
}

@media (max-width: 560px) {
  .review-card__header,
  .review-card__actions {
    align-items: stretch;
    flex-direction: column;
  }

  .review-card__badge {
    align-self: flex-start;
  }

  .review-card__actions .button {
    width: 100%;
  }

  .clarification {
    align-items: flex-start;
  }
}
</style>
