<template>
  <section class="capabilities" aria-labelledby="capabilities-title" data-home-reveal>
    <header class="capabilities__heading">
      <h2 id="capabilities-title">从数据中可以查看</h2>
      <p>可用条件随数据能力摘要更新。</p>
    </header>

    <p v-if="!capabilities && !capabilitiesLoaded" class="capabilities__state" aria-live="polite">正在读取当前数据能力…</p>
    <p v-else-if="!capabilities" class="capabilities__state" role="status">暂时无法读取能力摘要，可直接描述问题；不支持的条件会在分析前确认。</p>
    <p v-else-if="!items.length" class="capabilities__state" role="status">当前没有可展示的筛选条件摘要，请直接描述你想了解的问题。</p>

    <ul v-else class="capabilities__list">
      <li v-for="item in items" :key="item.key">
        <button type="button" class="capability" @click="emit('select', item.query)">
          <span class="capability__name">{{ item.label }}</span>
          <span class="capability__description">{{ item.description }}</span>
          <span class="capability__action">写入问题 <ArrowUpRight :size="15" aria-hidden="true" /></span>
        </button>
      </li>
    </ul>

    <p v-if="unsupportedLabels.length" class="capabilities__limitation">
      当前数据摘要标记为暂不支持：{{ unsupportedLabels.join('、') }}。分析前会核实这些条件。
    </p>
  </section>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { ArrowUpRight } from 'lucide-vue-next'
import type { DataCapabilities } from '../../services/analysisAdapter'

const props = defineProps<{ capabilities: DataCapabilities | null; capabilitiesLoaded: boolean }>()
const emit = defineEmits<{ select: [query: string] }>()

const definitions: Record<string, { label: string; description: string; query: string }> = {
  budget: { label: '预算', description: '按预算范围筛选挂牌样本。', query: '按预算筛选二手车挂牌样本，并说明价格分布' },
  city: { label: '城市', description: '比较不同城市的挂牌样本。', query: '比较不同城市的二手车挂牌价格与样本量' },
  brand: { label: '品牌', description: '查看品牌维度的挂牌数量与价格分布。', query: '查看各品牌二手车的挂牌数量与价格分布' },
  series: { label: '车系', description: '查看指定车系的挂牌价格分布。', query: '查看奥迪 A4L 二手车的挂牌价格分布' },
  year: { label: '生产年份', description: '比较不同生产年份的挂牌样本。', query: '比较不同生产年份二手车的挂牌价格分布' },
  energy_type: { label: '能源类型', description: '比较不同能源类型的挂牌样本。', query: '比较不同能源类型二手车挂牌价格与样本量' },
  max_age: { label: '车龄', description: '按车龄范围筛选挂牌样本。', query: '按车龄筛选二手车挂牌样本，查看价格分布' },
}
const labels: Record<string, string> = { body_type: '车型类别', drive_type: '驱动形式' }
const items = computed(() => (props.capabilities?.supported_filters ?? [])
  .map((key) => ({ key, ...(definitions[key] ?? { label: key, description: '按当前数据能力摘要识别。', query: `查看${key}条件下的二手车挂牌样本与价格分布` }) })))
const unsupportedLabels = computed(() => (props.capabilities?.unsupported_filters ?? []).map((key) => labels[key] || definitions[key]?.label || key))
</script>

<style scoped>
.capabilities { display: grid; grid-template-columns: minmax(210px, .7fr) minmax(0, 1.3fr); align-items: start; gap: clamp(28px, 6vw, 86px); }
.capabilities__heading { display: grid; gap: 14px; }
.capabilities__heading h2 { max-width: 410px; margin: 0; color: #202821; font-size: clamp(31px, 4vw, 54px); font-weight: 560; letter-spacing: -.035em; line-height: 1.05; text-wrap: balance; }
.capabilities__heading p { max-width: 260px; margin: 0; color: #46554b; font-size: 13px; line-height: 1.65; }
.capabilities__state, .capabilities__limitation { margin: 0; color: #46554b; font-size: 13px; line-height: 1.7; }
.capabilities__list { display: grid; margin: 0; padding: 0; list-style: none; border-top: 1px solid rgb(32 40 33 / .3); }
.capabilities__list li { min-width: 0; border-bottom: 1px solid rgb(32 40 33 / .22); }
.capability { display: grid; width: 100%; min-height: 74px; grid-template-columns: minmax(120px, .48fr) minmax(0, 1fr) auto; align-items: center; gap: 16px; padding: 13px 2px; color: inherit; text-align: left; border: 0; background: transparent; cursor: pointer; }
.capability__name { color: #254d3b; font-size: 15px; font-weight: 650; }
.capability__description { color: #46554b; font-size: 13px; line-height: 1.55; }
.capability__action { display: inline-flex; align-items: center; gap: 3px; color: #254d3b; font-size: 11px; white-space: nowrap; }
.capability:focus-visible { outline: 3px solid #bb5739; outline-offset: 2px; }
.capabilities__limitation { grid-column: 2; padding-top: 2px; }
@media (hover: hover) and (pointer: fine) { .capability:hover .capability__action { color: #9e452e; }.capability__action { transition: color 160ms ease; } }
@media (max-width: 760px) { .capabilities { grid-template-columns: 1fr; gap: 24px; }.capabilities__heading h2 { max-width: 600px; }.capabilities__heading p { max-width: 500px; }.capabilities__limitation { grid-column: 1; } }
@media (max-width: 520px) { .capability { grid-template-columns: minmax(80px, .38fr) minmax(0, 1fr) auto; gap: 5px 10px; padding-block: 14px; }.capability__name { align-self: end; }.capability__description { grid-column: 2; }.capability__action { grid-column: 3; grid-row: 1 / 3; } }
@media (max-width: 390px) { .capability { grid-template-columns: 1fr auto; }.capability__name, .capability__description { grid-column: 1; }.capability__action { grid-column: 2; grid-row: 1 / 3; } }
@media (prefers-reduced-motion: reduce) { .capability__action { transition: none; } }
</style>
