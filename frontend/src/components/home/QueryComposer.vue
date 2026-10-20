<template>
  <form class="composer" aria-labelledby="query-title" :aria-busy="busy || undefined" @submit.prevent="handleSubmit">
    <div class="composer__topline">
      <label id="query-title" for="query-input">写下你的问题</label>
      <details class="composer__capability">
        <summary>能力与限制</summary>
        <span class="composer__hint" aria-live="polite">{{ capabilityHint }}</span>
      </details>
    </div>

    <textarea
      id="query-input"
      :value="modelValue"
      name="query"
      rows="3"
      placeholder="例如：预算 15 万以内，想看看适合通勤的纯电车型"
      @input="handleInput"
    ></textarea>

    <div class="composer__bottomline">
      <p class="composer__helper">数字来自项目挂牌数据与统计工具</p>
      <button class="composer__submit" type="submit" :disabled="busy || !modelValue.trim()">
        <span>{{ busy ? '正在理解…' : '开始分析' }}</span>
        <ArrowUpRight :size="18" aria-hidden="true" />
      </button>
    </div>

    <Transition name="state-fade">
      <p v-if="error" class="field-error" role="alert">
        {{ error }} <code v-if="errorCode" class="field-error__code">{{ errorCode }}</code>
      </p>
    </Transition>
    <small v-if="error && (errorRequestId || errorStage)" class="field-error__diagnostic">
      阶段：{{ errorStage || '未知' }}<template v-if="errorRequestId"> · 请求 ID：{{ errorRequestId }}</template>
    </small>

    <div class="examples" aria-label="示例问题">
      <span class="examples__label">从一个问题开始</span>
      <button v-for="example in examples" :key="example" type="button" class="example" @click="emit('selectExample', example)">
        {{ example }} <ArrowUpRight :size="14" aria-hidden="true" />
      </button>
    </div>
  </form>
</template>

<script setup lang="ts">
import { ArrowUpRight } from 'lucide-vue-next'
import { computed } from 'vue'
import type { DataCapabilities } from '../../services/analysisAdapter'

const props = defineProps<{
  modelValue: string
  busy?: boolean
  error?: string
  errorCode?: string
  errorRequestId?: string
  errorStage?: string
  capabilities?: DataCapabilities | null
  capabilitiesLoaded?: boolean
}>()

const filterLabels: Record<string, string> = {
  budget: '预算', city: '城市', brand: '品牌', series: '车系',
  year: '生产年份', energy_type: '能源类型', max_age: '车龄',
}
const capabilityHint = computed(() => {
  if (!props.capabilities) {
    return props.capabilitiesLoaded
      ? '暂时无法读取能力摘要；不支持的条件会在分析前向你确认。'
      : '正在读取当前数据能力…'
  }
  const supported = props.capabilities.supported_filters.map((key) => filterLabels[key] || key)
  const unsupported = props.capabilities.unsupported_filters.map((key) => key === 'body_type' ? '车型类别' : key === 'drive_type' ? '驱动形式' : key)
  const summary = supported.length ? `可识别条件：${supported.join('、')}` : '暂未提供可筛选条件摘要'
  return unsupported.length ? `${summary}；暂不支持：${unsupported.join('、')}` : summary
})

const emit = defineEmits<{
  'update:modelValue': [value: string]
  submit: [value: string]
  selectExample: [value: string]
}>()

const examples = computed(() => [
  `15 万以内、3 年内的${props.capabilities?.energy_values[0] || '二手'}车型推荐几款`,
  '查看奥迪 A4L 的挂牌价格分布和样本情况',
  '比较宝马 3 系不同年份的挂牌价格',
])

function handleInput(event: Event) {
  emit('update:modelValue', (event.target as HTMLTextAreaElement).value)
}

function handleSubmit() {
  const value = props.modelValue.trim()
  if (value && !props.busy) emit('submit', value)
}
</script>

<style scoped>
.composer { min-width: 0; padding: clamp(20px, 3vw, 36px); border: 1px solid rgb(32 40 33 / .26); background: #f8f7f3; }
.composer__topline { display: flex; align-items: center; justify-content: space-between; gap: 20px; }
.composer__topline label { color: #202821; font-size: 15px; font-weight: 650; }
.composer__capability { position: relative; max-width: 100%; color: #46554b; font-size: 12px; }
.composer__capability summary { display: flex; min-height: 44px; align-items: center; justify-content: end; color: #254d3b; text-align: right; cursor: pointer; text-underline-offset: 4px; }
.composer__hint { display: block; max-width: min(500px, 70vw); padding: 8px 0 0; color: #46554b; font-size: 12px; line-height: 1.55; text-align: right; }
textarea { display: block; width: 100%; min-height: 126px; margin: 18px 0 20px; padding: 0; color: #202821; resize: vertical; border: 0; border-radius: 0; outline: 0; background: transparent; font: inherit; font-size: clamp(18px, 2vw, 25px); line-height: 1.55; caret-color: #254d3b; }
textarea::placeholder { color: #59665d; opacity: 1; }
textarea:focus-visible { outline: 2px solid #254d3b; outline-offset: 6px; }
.composer__bottomline { display: flex; align-items: center; justify-content: space-between; gap: 14px; padding-top: 16px; border-top: 1px solid rgb(32 40 33 / .22); }
.composer__helper { margin: 0; color: #46554b; font-size: 12px; line-height: 1.5; }
.composer__submit { display: inline-flex; min-height: 50px; align-items: center; justify-content: center; gap: 20px; padding: 0 18px; color: #fff; border: 0; border-radius: 2px; background: #254d3b; font: inherit; font-size: 14px; font-weight: 650; cursor: pointer; transition: background-color 180ms ease, transform 180ms ease; }
.composer__submit:disabled { cursor: not-allowed; opacity: .48; }
.composer__submit:focus-visible, .example:focus-visible { outline: 3px solid #bb5739; outline-offset: 3px; }
.field-error { margin: 13px 0 0; color: #9e3e2a; font-size: 13px; line-height: 1.55; }
.field-error__code { margin-left: 4px; font-size: 11px; font-weight: 650; }
.field-error__diagnostic { display: block; margin-top: 5px; color: #46554b; font-size: 12px; }
.examples { display: grid; grid-template-columns: auto repeat(3, minmax(0, 1fr)); align-items: start; gap: 12px 18px; margin-top: 24px; }
.examples__label { padding-top: 11px; color: #46554b; font-size: 11px; white-space: nowrap; }
.example { display: inline-flex; min-height: 44px; align-items: center; gap: 4px; padding: 6px 0; color: #254d3b; text-align: left; border: 0; border-bottom: 1px solid rgb(37 77 59 / .32); background: transparent; font: inherit; font-size: 12px; line-height: 1.45; cursor: pointer; transition: color 160ms ease, border-color 160ms ease; }
@media (hover: hover) and (pointer: fine) { .composer__submit:hover:not(:disabled) { transform: translateY(-1px); background: #1b3c2d; }.example:hover { color: #9e452e; border-color: #bb5739; } }
@media (max-width: 900px) { .examples { grid-template-columns: repeat(2, minmax(0, 1fr)); }.examples__label { grid-column: 1 / -1; padding: 0; } }
@media (max-width: 760px) { .composer__topline { align-items: flex-start; flex-direction: column; gap: 2px; width: 100%; }.composer__capability { width: 100%; }.composer__capability summary { justify-content: flex-start; }.composer__hint { max-width: none; text-align: left; } }
@media (max-width: 520px) { .composer { padding: 18px; }.composer__bottomline { align-items: stretch; flex-direction: column; }.composer__submit { width: 100%; }.examples { grid-template-columns: 1fr; gap: 4px; }.examples__label { grid-column: auto; margin: 0 0 4px; }.example { width: fit-content; max-width: 100%; } }
@media (prefers-reduced-motion: reduce) { .composer__submit, .example { transition: none; }.composer__submit:hover:not(:disabled) { transform: none; } }
</style>
