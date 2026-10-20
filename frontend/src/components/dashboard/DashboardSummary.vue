<template>
  <section class="summary" aria-label="筛选范围概览">
    <div class="summary__heading"><div><h2>当前样本</h2><p>{{ description }}</p></div><span v-if="datasetVersion" class="version">数据版本 {{ shortVersion }}</span></div>
    <dl v-if="overview" class="summary__metrics">
      <div><dt>数据集车源</dt><dd>{{ format(overview.dataset_rows) }}</dd></div>
      <div><dt>筛选后车源</dt><dd>{{ format(overview.scope_rows) }}</dd></div>
      <div><dt>有效挂牌价</dt><dd>{{ format(overview.price_valid_rows) }}</dd></div>
      <div><dt>有效城市</dt><dd>{{ format(overview.city_valid_rows) }}</dd></div>
      <div><dt>有车龄记录</dt><dd>{{ format(overview.vehicle_age_valid_rows) }}</dd></div>
    </dl>
    <div v-else class="summary__loading" role="status">{{ loading ? '正在读取筛选范围…' : '应用筛选后显示当前样本。' }}</div>
  </section>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { DashboardOverview } from '../../types/dashboard'
const props = defineProps<{ overview: DashboardOverview | null; datasetVersion?: string; loading?: boolean; description?: string }>()
const shortVersion = computed(() => props.datasetVersion ? props.datasetVersion.slice(0, 20) : '')
function format(value: number | undefined) { return typeof value === 'number' ? new Intl.NumberFormat('zh-CN').format(value) : '—' }
</script>

<style scoped>
.summary { display: grid; gap: 15px; padding: 17px 20px; border: 1px solid var(--color-border); border-radius: 14px; background: var(--color-surface); }
.summary__heading { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; }
h2 { margin: 0; color: var(--color-navy); font-size: 17px; }
.summary__heading p { margin: 4px 0 0; color: var(--color-ink-muted); font-size: 12px; line-height: 1.5; }
.version { color: var(--color-ink-muted); font-family: var(--font-mono); font-size: 10px; overflow-wrap: anywhere; }
.summary__metrics { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 12px; margin: 0; }
.summary__metrics div { display: grid; gap: 4px; padding-right: 12px; border-right: 1px solid var(--color-border); }
.summary__metrics div:last-child { border-right: 0; }
dt { color: var(--color-ink-muted); font-size: 11px; }
dd { margin: 0; color: var(--color-navy); font-size: 19px; font-variant-numeric: tabular-nums; font-weight: 750; }
.summary__loading { color: var(--color-ink-muted); font-size: 12px; }
@media (max-width: 760px) { .summary__metrics { grid-template-columns: repeat(3, minmax(0, 1fr)); } .summary__metrics div:nth-child(3) { border-right: 0; } }
@media (max-width: 480px) { .summary { padding: 14px; } .summary__heading { align-items: flex-start; flex-direction: column; } .summary__metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); } .summary__metrics div:nth-child(3) { border-right: 1px solid var(--color-border); } .summary__metrics div:nth-child(even) { border-right: 0; } dd { font-size: 17px; } }
</style>
