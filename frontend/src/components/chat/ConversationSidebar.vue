<template>
  <aside ref="sidebar" class="conversation-sidebar" :class="{ 'conversation-sidebar--open': open }" :inert="isMobile && !open" :aria-hidden="isMobile && !open ? 'true' : undefined" aria-label="历史会话" @keydown="onKeydown">
    <div class="conversation-sidebar__top">
      <div class="conversation-sidebar__brand"><strong>分析会话</strong></div>
      <button class="icon-button conversation-sidebar__close" type="button" aria-label="关闭会话列表" @click="emit('close')"><X :size="18" /></button>
      <button class="new-conversation" type="button" @click="emit('new')"><Plus :size="16" /><span>新建会话</span></button>
    </div>

    <div class="conversation-sidebar__list-wrap">
      <p class="conversation-sidebar__label">最近的会话</p>
      <div v-if="loading && !items.length" class="conversation-sidebar__loading" role="status"><LoaderCircle :size="16" class="spin" />正在加载</div>
      <p v-else-if="!items.length" class="conversation-sidebar__empty">还没有会话。开始一次分析后，它会保存在这里。</p>
      <nav v-else class="conversation-list" aria-label="会话列表">
        <button
          v-for="item in items"
          :key="item.conversationId"
          type="button"
          class="conversation-item"
          :class="{ 'conversation-item--active': item.conversationId === currentConversationId }"
          :aria-current="item.conversationId === currentConversationId ? 'page' : undefined"
          @click="emit('select', item.conversationId)"
        >
          <MessageSquare :size="15" aria-hidden="true" />
          <span class="conversation-item__copy"><strong>{{ item.title }}</strong><small>{{ item.latestEntryPreview || statusText(item) }}</small></span>
          <span v-if="item.hasPendingInput" class="conversation-item__dot" aria-label="等待回答"></span>
          <span v-else-if="item.activeRunStatus === 'running' || item.activeRunStatus === 'queued'" class="conversation-item__spinner" aria-label="运行中"></span>
        </button>
      </nav>
      <p v-if="error" class="conversation-sidebar__error" role="alert">{{ error }}</p>
      <button v-if="error" class="conversation-sidebar__more" type="button" :disabled="loading" @click="emit('load-more')">重试加载</button>
      <button v-else-if="hasMore" class="conversation-sidebar__more" type="button" :disabled="loading" @click="emit('load-more')">{{ loading ? '正在加载…' : '加载更早会话' }}</button>
    </div>

    <div class="conversation-sidebar__bottom"><small>每次分析，保留依据。</small></div>
  </aside>
  <Transition name="sidebar-scrim"><button v-if="open" class="conversation-sidebar__scrim" type="button" aria-label="关闭会话列表" @click="emit('close')"></button></Transition>
</template>

<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { LoaderCircle, MessageSquare, Plus, X } from 'lucide-vue-next'
import type { ConversationSummary } from '../../types/conversation'

const props = defineProps<{ items: ConversationSummary[]; currentConversationId: string | null; loading?: boolean; open?: boolean; hasMore?: boolean; error?: string }>()
const emit = defineEmits<{ new: []; select: [conversationId: string]; close: []; 'load-more': [] }>()
const sidebar = ref<HTMLElement | null>(null)
const isMobile = ref(false)
let mediaQuery: MediaQueryList | null = null
let previousFocus: HTMLElement | null = null

function updateMobile() { isMobile.value = Boolean(mediaQuery?.matches) }
onMounted(() => {
  mediaQuery = window.matchMedia('(max-width: 760px)')
  updateMobile()
  mediaQuery.addEventListener?.('change', updateMobile)
})
onBeforeUnmount(() => mediaQuery?.removeEventListener?.('change', updateMobile))
watch(() => props.open && isMobile.value, async (open) => {
  if (!open) {
    await nextTick()
    if (previousFocus?.isConnected) previousFocus.focus({ preventScroll: true })
    previousFocus = null
    return
  }
  previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null
  await nextTick()
  sidebar.value?.querySelector<HTMLElement>('.conversation-sidebar__close')?.focus()
})

function onKeydown(event: KeyboardEvent) {
  if (!isMobile.value || !props.open || !sidebar.value) return
  if (event.key === 'Escape') { event.preventDefault(); emit('close'); return }
  if (event.key !== 'Tab') return
  const items = [...sidebar.value.querySelectorAll<HTMLElement>('button:not(:disabled), a[href]')]
  if (!items.length) { event.preventDefault(); return }
  const first = items[0]
  const last = items[items.length - 1]
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus() }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus() }
}

function statusText(item: ConversationSummary): string {
  if (item.hasPendingInput) return '等待你的回答'
  if (item.activeRunStatus === 'running' || item.activeRunStatus === 'queued') return '正在分析'
  if (item.latestReport) return `分析报告 · v${item.latestReport.version}`
  return new Date(item.updatedAt).toLocaleDateString('zh-CN', { month: 'numeric', day: 'numeric' })
}
</script>

<style scoped>
.conversation-sidebar { position: relative; z-index: 30; display: flex; width: 264px; height: 100%; flex-direction: column; flex: 0 0 264px; color: var(--color-ink); border-right: 1px solid var(--color-border); background: var(--color-canvas); }
.conversation-sidebar__top { display: grid; gap: 20px; padding: 20px 14px 16px; }
.conversation-sidebar__brand { display: flex; align-items: center; gap: 10px; padding: 2px 5px; color: var(--color-navy); font-size: 14px; }
.conversation-sidebar__brand > span { display: grid; width: 27px; height: 27px; place-items: center; color: #fff; border-radius: 9px; background: var(--color-navy); font-family: var(--font-mono); font-size: 14px; font-weight: 800; }
.new-conversation { display: flex; align-items: center; gap: 9px; min-height: 42px; padding: 0 11px; color: var(--color-navy); border: 1px solid var(--color-border-strong); border-radius: 10px; background: #fff; box-shadow: 0 1px 2px rgb(15 45 70 / 0.04); font-size: 13px; font-weight: 700; text-align: left; }
.new-conversation:hover { border-color: var(--color-teal); background: var(--color-teal-soft); }
.conversation-sidebar__list-wrap { min-height: 0; flex: 1; overflow-y: auto; padding: 4px 9px 16px; }
.conversation-sidebar__label { margin: 7px 8px 9px; color: var(--color-ink-muted); font-size: 11px; font-weight: 750; letter-spacing: .04em; text-transform: uppercase; }
.conversation-list { display: grid; gap: 3px; }
.conversation-item { display: flex; width: 100%; align-items: center; gap: 9px; min-height: 48px; padding: 7px 9px; color: var(--color-ink-soft); border: 1px solid transparent; border-radius: 9px; background: transparent; text-align: left; }
.conversation-item:hover { color: var(--color-navy); background: #fff; }
.conversation-item--active { color: var(--color-navy); border-color: rgb(15 45 70 / .08); background: #fff; box-shadow: 0 2px 8px rgb(15 45 70 / .04); }
.conversation-item > svg { flex: 0 0 15px; color: var(--color-teal); }
.conversation-item__copy { display: grid; min-width: 0; flex: 1; gap: 3px; }
.conversation-item__copy strong, .conversation-item__copy small { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.conversation-item__copy strong { font-size: 12px; font-weight: 680; }
.conversation-item__copy small { color: var(--color-ink-muted); font-size: 10px; }
.conversation-item__dot { width: 7px; height: 7px; flex: 0 0 7px; border-radius: 50%; background: var(--color-orange); }
.conversation-item__spinner, .spin { animation: sidebar-spin 1s linear infinite; }
.conversation-item__spinner { width: 12px; height: 12px; flex: 0 0 12px; border: 1.5px solid var(--color-border-strong); border-top-color: var(--color-teal); border-radius: 50%; }
.conversation-sidebar__loading { display: flex; align-items: center; gap: 8px; padding: 10px; color: var(--color-ink-muted); font-size: 12px; }
.conversation-sidebar__empty { margin: 5px 8px; color: var(--color-ink-muted); font-size: 12px; line-height: 1.6; }
.conversation-sidebar__more { width: 100%; min-height: 40px; margin-top: 8px; padding: 5px 8px; color: var(--color-navy); border: 1px solid var(--color-border); border-radius: 8px; background: #fff; font-size: 12px; cursor: pointer; }
.conversation-sidebar__more:disabled { opacity: .6; cursor: wait; }
.conversation-sidebar__error { margin: 8px; color: var(--color-danger); font-size: 12px; line-height: 1.5; }
.conversation-sidebar__bottom { padding: 11px 15px max(14px, env(safe-area-inset-bottom)); border-top: 1px solid var(--color-border); color: var(--color-ink-muted); font-size: 12px; }
.sidebar-link { display: flex; align-items: center; gap: 8px; min-height: 38px; color: var(--color-ink-muted); font-size: 12px; text-decoration: none; }
.sidebar-link:hover { color: var(--color-navy); }
.conversation-sidebar__close, .conversation-sidebar__scrim { display: none; }
@keyframes sidebar-spin { to { transform: rotate(360deg); } }
@media (max-width: 760px) {
  .conversation-sidebar { position: fixed; inset: var(--topbar-height) auto 0 0; width: min(300px, calc(100vw - 54px)); height: calc(100dvh - var(--topbar-height)); transform: translateX(-105%); transition: transform 220ms var(--ease-standard); box-shadow: 20px 0 60px rgb(32 40 33 / .12); }
  .conversation-sidebar--open { transform: translateX(0); }
  .conversation-sidebar__close { display: inline-grid; position: absolute; top: 20px; right: 14px; width: 30px; height: 30px; place-items: center; color: var(--color-ink-muted); border: 0; border-radius: 8px; background: transparent; }
  .conversation-sidebar__scrim { display: block; position: fixed; z-index: 25; inset: var(--topbar-height) 0 0; width: 100%; height: calc(100% - var(--topbar-height)); border: 0; background: rgb(32 40 33 / .24); }
}
@media (prefers-reduced-motion: reduce) { .conversation-sidebar, .conversation-sidebar__spinner, .spin { transition: none; animation: none; } }
</style>
