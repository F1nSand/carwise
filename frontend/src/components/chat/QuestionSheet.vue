<template>
  <Teleport to="body">
    <Transition name="question-sheet">
      <div v-if="open" class="question-sheet__backdrop" @mousedown.self="close">
        <section ref="dialog" class="question-sheet" role="dialog" aria-modal="true" aria-labelledby="question-sheet-title" aria-describedby="question-sheet-description" tabindex="-1" @keydown="onKeydown">
          <header class="question-sheet__header">
            <div><span class="question-sheet__eyebrow">需要你确认</span><h2 id="question-sheet-title">回答后 Agent 会继续当前计划</h2></div>
            <button class="question-sheet__close" type="button" @click="close">暂时收起 <X :size="16" aria-hidden="true" /></button>
          </header>
          <AgentQuestionCard
            :pending-input="pendingInput"
            :busy="busy"
            :submit-error="submitError"
            :identity="identity"
            :draft="draft"
            @update:draft="emit('update:draft', $event)"
            @submit="emit('submit', $event)"
          />
          <p id="question-sheet-description" class="question-sheet__footnote">收起后任务会继续等待；你的选择会保留在这个会话中。</p>
        </section>
      </div>
    </Transition>
  </Teleport>
</template>

<script setup lang="ts">
import { nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { X } from 'lucide-vue-next'
import AgentQuestionCard from '../analysis/AgentQuestionCard.vue'
import type { AgentAnswer, PendingInput } from '../../types/analysis'
import type { PendingQuestionDraft } from '../../types/conversation'

const props = defineProps<{
  open: boolean
  pendingInput: PendingInput
  identity: string
  draft: PendingQuestionDraft | null
  busy?: boolean
  submitError?: string
}>()
const emit = defineEmits<{
  'update:open': [open: boolean]
  'update:draft': [draft: PendingQuestionDraft]
  submit: [answers: AgentAnswer[]]
}>()

const dialog = ref<HTMLElement | null>(null)
let previousFocus: HTMLElement | null = null
let previousBodyOverflow = ''
let bodyLocked = false

watch(() => props.open, async (open) => {
  if (!open) {
    await nextTick()
    restoreFocus()
    return
  }
  previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null
  previousBodyOverflow = document.body.style.overflow
  document.body.style.overflow = 'hidden'
  bodyLocked = true
  await nextTick()
  focusInitialTarget()
}, { immediate: true })

watch(() => props.identity, async () => {
  if (!props.open) return
  await nextTick()
  focusInitialTarget()
})

onBeforeUnmount(() => restoreFocus())

function focusInitialTarget() {
  const target = dialog.value?.querySelector<HTMLElement>(
    '.option-button:not(:disabled), .question-card__actions button:not(:disabled)',
  ) || dialog.value
  target?.focus()
}

function restoreFocus() {
  if (!bodyLocked) return
  document.body.style.overflow = previousBodyOverflow
  bodyLocked = false
  const target = previousFocus?.isConnected
    ? previousFocus
    : document.querySelector<HTMLElement>('#chat-message')
  target?.focus()
  previousFocus = null
}

function close() {
  emit('update:open', false)
}

function onKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') {
    event.preventDefault()
    close()
    return
  }
  if (event.key !== 'Tab' || !dialog.value) return
  const items = [...dialog.value.querySelectorAll<HTMLElement>(
    'button:not(:disabled), input:not(:disabled), textarea:not(:disabled), select:not(:disabled), a[href], [tabindex]:not([tabindex="-1"])',
  )]
    .filter((item) => item.offsetParent !== null)
  if (!items.length) { event.preventDefault(); dialog.value.focus(); return }
  const first = items[0]
  const last = items[items.length - 1]
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus() }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus() }
}
</script>

<style scoped>
.question-sheet__backdrop { position: fixed; z-index: 120; inset: 0; display: flex; align-items: flex-end; justify-content: center; padding: 24px; background: rgb(12 31 48 / .32); backdrop-filter: blur(3px); }
.question-sheet { width: min(620px, 100%); max-height: min(84dvh, 760px); overflow: auto; padding: 20px; border: 1px solid rgb(15 45 70 / .12); border-radius: 18px; background: #fff; box-shadow: 0 24px 90px rgb(15 45 70 / .24); outline: none; }
.question-sheet__header { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; padding: 2px 3px 15px; }
.question-sheet__eyebrow { color: var(--color-orange); font-size: 10px; font-weight: 750; letter-spacing: .04em; }
.question-sheet__header h2 { margin: 5px 0 0; color: var(--color-navy); font-size: 15px; letter-spacing: -.025em; }
.question-sheet__close { display: inline-flex; align-items: center; gap: 5px; min-height: 32px; padding: 0 8px; color: var(--color-ink-muted); border: 1px solid var(--color-border); border-radius: 8px; background: #fff; font-size: 10px; white-space: nowrap; }
.question-sheet__close:hover { color: var(--color-navy); background: var(--color-surface-subtle); }
.question-sheet__footnote { margin: 12px 3px 0; color: var(--color-ink-muted); font-size: 10px; }
.question-sheet-enter-active, .question-sheet-leave-active { transition: opacity 180ms ease; }
.question-sheet-enter-active .question-sheet, .question-sheet-leave-active .question-sheet { transition: transform 320ms cubic-bezier(.2,.75,.25,1); }
.question-sheet-enter-from, .question-sheet-leave-to { opacity: 0; }
.question-sheet-enter-from .question-sheet, .question-sheet-leave-to .question-sheet { transform: translateY(28px); }
@media (max-width: 620px) {
  .question-sheet__backdrop { padding: 0; }
  .question-sheet { width: 100%; max-height: 88dvh; padding: 17px 14px max(17px, env(safe-area-inset-bottom)); border-radius: 18px 18px 0 0; }
  .question-sheet__header { padding-right: 3px; padding-left: 3px; }
}
@media (prefers-reduced-motion: reduce) { .question-sheet-enter-active, .question-sheet-leave-active, .question-sheet-enter-active .question-sheet, .question-sheet-leave-active .question-sheet { transition: none; } }
</style>
