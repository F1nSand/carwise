<template>
  <form class="chat-composer" aria-label="发送消息" @submit.prevent="submit">
    <label class="sr-only" for="chat-message">继续询问或补充条件</label>
    <textarea
      id="chat-message"
      ref="input"
      :value="modelValue"
      rows="1"
      :placeholder="busy ? '补充条件，调整当前分析…' : '继续追问，或输入新的分析问题…'"
      :disabled="disabled"
      @input="emit('update:modelValue', ($event.target as HTMLTextAreaElement).value)"
      @keydown="onKeydown"
      @compositionstart="composing = true"
      @compositionend="composing = false"
    ></textarea>
    <div class="chat-composer__footer">
      <span><span class="chat-composer__dot" aria-hidden="true"></span>{{ busy ? '发送后会在安全节点调整当前任务' : '回答基于当前会话与挂牌数据' }}</span>
      <button type="submit" class="chat-composer__send" :disabled="disabled || !modelValue.trim()" :aria-label="busy ? '发送追加指令' : '发送消息'">
        <span>{{ busy ? '调整任务' : '发送' }}</span><ArrowUp :size="17" aria-hidden="true" />
      </button>
    </div>
  </form>
</template>

<script setup lang="ts">
import { nextTick, ref } from 'vue'
import { ArrowUp } from 'lucide-vue-next'

defineProps<{ modelValue: string; busy?: boolean; disabled?: boolean }>()
const emit = defineEmits<{ 'update:modelValue': [value: string]; submit: [value: string] }>()
const input = ref<HTMLTextAreaElement | null>(null)
const composing = ref(false)

function onKeydown(event: KeyboardEvent) {
  if (event.key === 'Enter' && !event.shiftKey && !composing.value && !event.isComposing && event.keyCode !== 229) {
    event.preventDefault()
    submit()
  }
}

function submit() {
  const text = input.value?.value.trim() || ''
  if (text) emit('submit', text)
}

async function focus() {
  await nextTick()
  input.value?.focus()
}

defineExpose({ focus })
</script>

<style scoped>
.chat-composer { display: grid; gap: 9px; padding: 12px 13px 10px; border: 1px solid rgb(15 45 70 / .15); border-radius: 16px; background: #fff; box-shadow: 0 8px 32px rgb(15 45 70 / .09); }
.chat-composer:focus-within { border-color: rgb(15 118 110 / .52); box-shadow: 0 0 0 3px rgb(15 118 110 / .08), 0 8px 32px rgb(15 45 70 / .09); }
textarea { display: block; width: 100%; min-height: 31px; max-height: min(180px, 22dvh); padding: 3px 5px; resize: vertical; overflow-y: auto; color: var(--color-ink); border: 0; outline: 0; background: transparent; font: inherit; font-size: 14px; line-height: 1.6; }
textarea::placeholder { color: #8996a3; }
textarea:disabled { opacity: .65; }
.chat-composer__footer { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.chat-composer__footer > span { display: flex; align-items: center; gap: 7px; color: var(--color-ink-muted); font-size: 10px; }
.chat-composer__dot { width: 6px; height: 6px; border-radius: 50%; background: var(--color-teal); }
.chat-composer__send { display: inline-flex; align-items: center; justify-content: center; gap: 7px; min-height: 34px; padding: 0 12px; color: #fff; border: 0; border-radius: 9px; background: var(--color-navy); font-size: 12px; font-weight: 720; transition: background-color 150ms ease, transform 150ms ease; }
.chat-composer__send:hover:not(:disabled) { background: var(--color-teal); transform: translateY(-1px); }
.chat-composer__send:disabled { cursor: not-allowed; opacity: .42; }
.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; border: 0; }
@media (prefers-reduced-motion: reduce) { .chat-composer__send { transition: none; } }
</style>
