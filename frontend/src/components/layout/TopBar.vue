<template>
  <header class="topbar">
    <div class="topbar__inner page-container">
      <RouterLink class="brand" to="/" aria-label="返回二手车智能分析首页">
        <span class="brand__mark" aria-hidden="true"><CarFront :size="25" :stroke-width="1.6" /></span>
        <span class="brand__copy"><strong>车市分析</strong><small>二手车智能分析</small></span>
      </RouterLink>
      <nav class="topbar__nav" aria-label="主导航">
        <RouterLink to="/" exact-active-class="topbar__link--active" class="topbar__link">智能分析</RouterLink>
        <RouterLink to="/dashboard" active-class="topbar__link--active" class="topbar__link">市场看板</RouterLink>
        <RouterLink to="/chat" class="topbar__link" :class="{ 'topbar__link--active': isChat }" :aria-current="isChat ? 'page' : undefined">聊天会话</RouterLink>
        <RouterLink to="/about" active-class="topbar__link--active" class="topbar__link">数据说明</RouterLink>
      </nav>
      <span class="topbar__note">看清行情，再做选择。</span>
    </div>
  </header>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { RouterLink, useRoute } from 'vue-router'
import { CarFront } from 'lucide-vue-next'
const route = useRoute()
const isChat = computed(() => route.name === 'conversation' || route.name === 'conversations' || route.path === '/chat' || route.path.startsWith('/chat/'))
</script>

<style scoped>
.topbar { position: fixed; inset: 0 0 auto; z-index: 40; height: var(--topbar-height); background: var(--color-canvas); border-bottom: 1px solid var(--color-border); }
.topbar__inner { display: grid; height: calc(var(--topbar-height) - 1px); grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr); align-items: center; gap: 20px; }
.brand { display: flex; align-items: center; gap: 11px; min-height: 44px; flex-shrink: 0; }
.brand__mark { display: grid; width: 43px; height: 43px; place-items: center; color: var(--color-teal); border: 1px solid var(--color-border-strong); border-radius: 50%; }
.brand__copy { display: grid; gap: 1px; }
.brand__copy strong { font-size: 20px; font-weight: 750; letter-spacing: -.03em; line-height: 1.3; }
.brand__copy small { color: var(--color-ink-muted); font-size: 11px; letter-spacing: .04em; }
.topbar__nav { display: grid; grid-template-columns: repeat(4, 76px); align-items: stretch; align-self: stretch; gap: 8px; }
.topbar__link { position: relative; display: inline-flex; align-items: center; justify-content: center; min-height: 44px; color: var(--color-ink-soft); font-size: 13px; white-space: nowrap; transition: color var(--duration-fast) ease; }
.topbar__link::after { position: absolute; inset: auto 0 0; height: 2px; background: var(--color-teal); transform: scaleX(0); transform-origin: left; transition: transform 220ms var(--ease-standard); content: ''; }
.topbar__link--active { color: var(--color-teal); font-weight: 700; }
.topbar__link--active::after { transform: scaleX(1); }
.topbar__note { justify-self: end; color: var(--color-ink-muted); font-size: 12px; white-space: nowrap; }
@media (hover: hover) { .topbar__link:hover { color: var(--color-teal); } .topbar__link:hover::after { transform: scaleX(1); } }
@media (max-width: 1000px) { .topbar__note { display: none; } }
@media (max-width: 760px) {
  .topbar__inner { grid-template-columns: minmax(0, 1fr); grid-template-rows: 53px 44px; gap: 0; }
  .brand { width: 100%; min-height: 53px; gap: 8px; }
  .brand__mark { width: 32px; height: 32px; }
  .brand__mark svg { width: 21px; }
  .brand__copy { display: flex; align-items: baseline; gap: 10px; }
  .brand__copy strong { font-size: 18px; }
  .brand__copy small { font-size: 10px; }
  .topbar__nav { width: 100%; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 0; }
  .topbar__link { min-height: 44px; padding: 0 8px; font-size: 12px; }
}
</style>
