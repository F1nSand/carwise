<template>
  <AppShell :chat-workspace="route.name === 'conversation' || route.name === 'conversations'" :home-landing="route.path === '/'">
    <div ref="routeHost" class="route-host">
    <RouterView v-slot="{ Component, route }">
      <Transition :name="transitionName" mode="out-in">
        <component :is="Component" :key="['chat', 'conversation', 'conversations'].includes(String(route.name)) ? 'chat-workspace' : route.path" />
      </Transition>
    </RouterView>
    </div>
  </AppShell>
</template>

<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { RouterView, useRoute } from 'vue-router'
import AppShell from './components/layout/AppShell.vue'

const route = useRoute()
const transitionName = ref('route-forward')
const routeHost = ref<HTMLElement | null>(null)
let workspaceTransition: Animation | null = null
let motionPreference: MediaQueryList | null = null
function stopWorkspaceMotion(event: MediaQueryListEvent) { if (event.matches) workspaceTransition?.cancel() }
onMounted(() => {
  motionPreference = window.matchMedia?.('(prefers-reduced-motion: reduce)') ?? null
  motionPreference?.addEventListener?.('change', stopWorkspaceMotion)
})
function pageOrder(path: string) { return path === '/' ? 0 : path.startsWith('/dashboard') ? 1 : path.startsWith('/chat') ? 2 : path.startsWith('/analysis') ? 3 : 4 }
watch(() => route.path, async (next, previous) => {
  transitionName.value = pageOrder(next) < pageOrder(previous) ? 'route-back' : 'route-forward'
  workspaceTransition?.cancel()
  workspaceTransition = null
  // Keep the chat component alive during first-message handoff. Its composer
  // owns that transition; plain navigation between home and chat uses this one.
  if (!((next === '/' && previous.startsWith('/chat')) || (previous === '/' && next === '/chat'))) return
  await nextTick()
  if (route.path !== next || window.matchMedia?.('(prefers-reduced-motion: reduce)').matches || !routeHost.value?.animate) return
  workspaceTransition = routeHost.value.animate([
    { transform: next === '/' ? 'translate3d(-24px, 8px, 0)' : 'translate3d(24px, 8px, 0)', clipPath: 'inset(0 0 5% 0)' },
    { transform: 'translate3d(0, 0, 0)', clipPath: 'inset(0 0 0 0)' },
  ], { duration: 260, easing: 'cubic-bezier(0.23, 1, 0.32, 1)' })
})
onBeforeUnmount(() => {
  workspaceTransition?.cancel()
  motionPreference?.removeEventListener?.('change', stopWorkspaceMotion)
})
</script>
