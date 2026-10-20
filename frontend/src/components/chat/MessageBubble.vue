<template>
  <article class="message" :class="`message--${role}`" :data-entry-id="entryId">
    <div v-if="role === 'assistant'" class="message__avatar" aria-hidden="true">Y</div>
    <div class="message__body">
      <span class="message__sender">{{ role === 'user' ? '你' : 'Agent' }}</span>
      <div class="message__bubble"><AgentMarkdownContent v-if="role === 'assistant' && markdown" :source="content" /><p v-else>{{ content }}</p></div>
      <small v-if="role === 'user' && deliveryState === 'sending'" class="message__delivery" role="status">正在发送</small>
      <small v-else-if="role === 'user' && deliveryState === 'failed'" class="message__delivery message__delivery--failed" role="alert">未发送 · 内容已放回输入框</small>
      <time v-if="timestamp" class="message__time" :datetime="timestamp">{{ formatTime(timestamp) }}</time>
    </div>
  </article>
</template>

<script setup lang="ts">
import AgentMarkdownContent from '../report/AgentMarkdownContent.vue'

withDefaults(defineProps<{ role: 'user' | 'assistant'; content: string; entryId?: string; timestamp?: string; markdown?: boolean; deliveryState?: string }>(), { markdown: false, deliveryState: '' })

function formatTime(value: string): string {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
}
</script>

<style scoped>
.message { display: flex; max-width: 800px; gap: 10px; margin: 8px auto; padding: 0 22px; animation: message-in 180ms ease both; }
.message--user { justify-content: flex-end; }
.message__avatar { display: grid; width: 27px; height: 27px; flex: 0 0 27px; place-items: center; color: #fff; border-radius: 9px; background: var(--color-navy); font-family: var(--font-mono); font-size: 12px; font-weight: 800; }
.message__body { display: grid; max-width: min(660px, 90%); gap: 5px; }
.message--user .message__body { justify-items: end; }
.message__sender { color: var(--color-ink-muted); font-size: 10px; font-weight: 680; }
.message__bubble { padding: 11px 14px; color: var(--color-ink); border: 1px solid var(--color-border); border-radius: 4px 14px 14px; background: #fff; font-size: 13px; line-height: 1.7; overflow-wrap: anywhere; }
.message--user .message__bubble { border-color: rgb(15 45 70 / .08); border-radius: 14px 4px 14px 14px; background: #edf4f5; }
.message__bubble :deep(p:first-child) { margin-top: 0; }
.message__bubble :deep(p:last-child) { margin-bottom: 0; }
.message__time { color: var(--color-ink-muted); font-size: 9px; }
.message__delivery { color: var(--color-ink-muted); font-size: 9px; }
.message__delivery--failed { color: var(--color-danger); }
@keyframes message-in { from { opacity: 0; transform: translateY(7px); } to { opacity: 1; transform: translateY(0); } }
@media (prefers-reduced-motion: reduce) { .message { animation: none; } }
</style>
