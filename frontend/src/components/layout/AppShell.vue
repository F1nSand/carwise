<template>
  <div class="app-shell" :class="{ 'app-shell--chat': chatWorkspace }">
    <a class="skip-link" href="#main-content">跳到主要内容</a>
    <TopBar />
    <main id="main-content" class="page-container app-shell__main" :class="{ 'app-shell__main--chat': chatWorkspace, 'app-shell__main--landing': homeLanding }" tabindex="-1">
      <slot />
    </main>
    <div class="sr-status" role="status" aria-live="polite">{{ statusMessage }}</div>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import TopBar from './TopBar.vue'

defineProps<{ chatWorkspace?: boolean; homeLanding?: boolean }>()
const statusMessage = ref('')
</script>

<style scoped>
.app-shell {
  min-height: 100dvh;
  padding-top: var(--topbar-height);
}

.app-shell--chat {
  display: flex;
  height: 100dvh;
  min-height: 0;
  flex-direction: column;
  overflow: hidden;
}

.app-shell__main {
  min-height: calc(100dvh - var(--topbar-height));
  padding-top: var(--space-8);
  padding-bottom: var(--space-16);
}

.app-shell__main:focus-visible {
  outline: 3px solid rgb(15 118 110 / 0.72);
  outline-offset: 4px;
}

.app-shell__main:focus:not(:focus-visible) {
  outline: none;
}

.app-shell__main--chat {
  flex: 1;
  width: 100%;
  max-width: none;
  min-height: 0;
  margin: 0;
  padding: 0;
  overflow: hidden;
}

.app-shell__main--chat :deep(.route-host) { width: 100%; height: 100%; min-height: 0; }

.app-shell__main--landing {
  width: 100%;
  max-width: none;
  padding: 0;
}

.skip-link {
  position: fixed;
  z-index: 100;
  top: 12px;
  left: 12px;
  padding: 10px 14px;
  color: #fff;
  border-radius: 8px;
  background: var(--color-navy);
  transform: translateY(-180%);
  transition: transform var(--duration-fast) var(--ease-standard);
}

.skip-link:focus {
  transform: translateY(0);
}

.sr-status {
  position: fixed;
  width: 1px;
  height: 1px;
  padding: 0;
  overflow: hidden;
  white-space: nowrap;
  border: 0;
  clip: rect(0 0 0 0);
  clip-path: inset(50%);
}

@media (max-width: 760px) {
  .app-shell__main {
    min-height: calc(100dvh - var(--topbar-height));
    padding-top: var(--space-8);
    padding-bottom: var(--space-12);
  }
  .app-shell__main--chat { min-height: 0; padding: 0; }
  .app-shell__main--landing { padding: 0; }
}
</style>
