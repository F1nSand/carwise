<template>
  <form class="run-correction" aria-labelledby="correction-title" @submit.prevent="submit">
    <div class="run-correction__heading">
      <div>
        <h3 id="correction-title">补充或修改条件</h3>
        <p>Agent 会在安全边界采用这条补充信息，并重新检查受影响的查询。</p>
      </div>
      <span v-if="status === 'received'" class="run-correction__status" role="status">已收到，等待采用</span>
      <span v-else-if="status === 'applied'" class="run-correction__status run-correction__status--applied" role="status">已采用</span>
    </div>

    <label class="sr-only" for="run-correction-input">补充或修改条件</label>
    <textarea
      id="run-correction-input"
      v-model="content"
      maxlength="500"
      :disabled="busy"
      placeholder="例如：预算改为 12 万以内，优先考虑油车。"
      rows="3"
    />
    <div class="run-correction__footer">
      <span>{{ content.length }}/500</span>
      <button class="button button--primary" type="submit" :disabled="busy || !content.trim()">
        {{ busy ? '正在发送…' : '发送补充条件' }}
      </button>
    </div>
    <p v-if="error" class="run-correction__error" role="alert">{{ error }}</p>
    <p v-else-if="status === 'submitting'" class="run-correction__hint" role="status">正在提交，重复点击不会生成另一条补充信息。</p>
  </form>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'
import type { CorrectionStatus } from '../../types/analysis'

const props = defineProps<{ busy: boolean; status: CorrectionStatus; error: string }>()
const emit = defineEmits<{ submit: [content: string] }>()
const content = ref('')

watch(() => props.status, (status) => {
  if (status === 'applied') content.value = ''
})

function submit() {
  const value = content.value.trim()
  if (value && value.length <= 500 && !props.busy) emit('submit', value)
}
</script>

<style scoped>
.run-correction { display: grid; align-content: start; gap: 12px; min-width: 0; padding: 20px; border: 1px solid var(--color-border); border-radius: 16px; background: var(--color-surface); }
.run-correction__heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
.run-correction h3 { margin: 0; color: var(--color-navy); font-size: 14px; }
.run-correction__heading p { margin: 6px 0 0; color: var(--color-ink-muted); font-size: 13px; line-height: 1.6; }
.run-correction textarea { width: 100%; min-height: 82px; padding: 11px 12px; color: var(--color-ink); border: 1px solid var(--color-border-strong); border-radius: 10px; background: #fff; font: inherit; font-size: 14px; line-height: 1.6; resize: vertical; }
.run-correction textarea:focus-visible { outline: 2px solid var(--color-teal); outline-offset: 2px; }
.run-correction textarea:disabled { opacity: 0.65; }
.run-correction__footer { display: flex; align-items: center; justify-content: space-between; gap: 12px; color: var(--color-ink-muted); font-size: 12px; }
.run-correction__status { flex: 0 0 auto; padding: 6px 8px; color: var(--color-orange); border-radius: 999px; background: var(--color-orange-soft); font-size: 12px; font-weight: 700; }
.run-correction__status--applied { color: var(--color-teal); background: var(--color-teal-soft); }
.run-correction__hint, .run-correction__error { margin: 0; font-size: 12px; line-height: 1.5; }
.run-correction__hint { color: var(--color-ink-muted); }
.run-correction__error { color: var(--color-danger); }
.button { display: inline-flex; align-items: center; justify-content: center; min-height: 44px; padding: 0 14px; color: #fff; border: 0; border-radius: 9px; background: var(--color-navy); font-size: 13px; font-weight: 750; cursor: pointer; }
.button:disabled { opacity: 0.55; cursor: not-allowed; }
.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }
@media (max-width: 420px) { .run-correction__heading { flex-direction: column; } }
</style>
