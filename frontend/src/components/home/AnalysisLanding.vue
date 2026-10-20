<template>
  <section ref="landing" class="landing" aria-labelledby="landing-title">
    <HomeAnalysisPreview
      :capabilities="capabilities"
      :capabilities-loaded="capabilitiesLoaded"
      @jump-to-composer="jumpToComposer"
    />

    <section class="landing__query-band" aria-label="开始一次分析" data-home-reveal>
      <div ref="query" class="landing__query">
        <div class="landing__query-heading">
          <h2>你正在考虑什么车？</h2>
          <p>写下预算、车型、年份或城市。分析结果会保留样本量、统计口径与限制。</p>
        </div>
        <QueryComposer
          :model-value="modelValue"
          :capabilities="capabilities"
          :capabilities-loaded="capabilitiesLoaded"
          :busy="busy"
          :error="error"
          @update:model-value="emit('update:modelValue', $event)"
          @submit="emit('submit', $event)"
          @select-example="selectExample"
        />
      </div>
    </section>

    <div class="landing__paper-content">
      <section class="landing__market">
        <HomeMarketLens
          :capabilities="capabilities"
          :capabilities-loaded="capabilitiesLoaded"
          @select="selectExample"
        />
      </section>

      <HomeCapabilities :capabilities="capabilities" :capabilities-loaded="capabilitiesLoaded" @select="selectExample" />

      <footer class="landing__note">
        <p>这里呈现的是历史挂牌快照。挂牌价格不等于实际成交价，也不替代实车检查。</p>
        <span>二手车数据分析</span>
      </footer>
    </div>
  </section>
</template>

<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import QueryComposer from './QueryComposer.vue'
import HomeAnalysisPreview from './HomeAnalysisPreview.vue'
import HomeMarketLens from './HomeMarketLens.vue'
import HomeCapabilities from './HomeCapabilities.vue'
import { getDataCapabilities, type DataCapabilities } from '../../services/analysisAdapter'

defineProps<{ modelValue: string; busy?: boolean; error?: string }>()
const emit = defineEmits<{ 'update:modelValue': [value: string]; submit: [value: string] }>()
const landing = ref<HTMLElement | null>(null)
const query = ref<HTMLElement | null>(null)
const capabilities = ref<DataCapabilities | null>(null)
const capabilitiesLoaded = ref(false)
let observer: IntersectionObserver | null = null
let capabilitiesController: AbortController | null = null
const activeAnimations = new Set<Animation>()
let motionPreference: MediaQueryList | null = null

function revealVisibleSections() {
  observer?.disconnect()
  observer = null
  activeAnimations.forEach((animation) => animation.cancel())
  activeAnimations.clear()
  if (motionPreference?.matches || !('IntersectionObserver' in window)) return
  observer = new IntersectionObserver((entries) => {
    for (const entry of entries) {
      if (!entry.isIntersecting) continue
      const element = entry.target as HTMLElement
      observer?.unobserve(element)
      if (element.dataset.revealed === 'true' || !element.animate) continue
      element.dataset.revealed = 'true'
      const animation = element.animate(
        [{ opacity: 0.72, transform: 'translateY(18px)', clipPath: 'inset(5% 0 0)' }, { opacity: 1, transform: 'translateY(0)', clipPath: 'inset(0 0 0)' }],
        { duration: 680, easing: 'cubic-bezier(0.23, 1, 0.32, 1)', fill: 'both' },
      )
      activeAnimations.add(animation)
      animation.onfinish = () => activeAnimations.delete(animation)
      animation.oncancel = () => activeAnimations.delete(animation)
    }
  }, { rootMargin: '0px 0px -12% 0px', threshold: 0.12 })
  landing.value?.querySelectorAll<HTMLElement>('[data-home-reveal]').forEach((element) => observer?.observe(element))
}

onMounted(() => {
  capabilitiesController = new AbortController()
  const request = capabilitiesController
  void getDataCapabilities(request.signal).then((value) => { capabilities.value = value })
    .catch(() => { if (!request.signal.aborted) capabilities.value = null })
    .finally(() => { if (!request.signal.aborted) capabilitiesLoaded.value = true })

  motionPreference = window.matchMedia?.('(prefers-reduced-motion: reduce)') ?? null
  motionPreference?.addEventListener?.('change', revealVisibleSections)
  revealVisibleSections()
})

onUnmounted(() => {
  capabilitiesController?.abort()
  capabilitiesController = null
  observer?.disconnect()
  activeAnimations.forEach((animation) => animation.cancel())
  activeAnimations.clear()
  motionPreference?.removeEventListener?.('change', revealVisibleSections)
})

function selectExample(value: string) {
  emit('update:modelValue', value)
  query.value?.querySelector<HTMLTextAreaElement>('textarea')?.focus()
}

function jumpToComposer() {
  const reducedMotion = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false
  query.value?.scrollIntoView?.({ behavior: reducedMotion ? 'auto' : 'smooth', block: 'start' })
  query.value?.querySelector<HTMLTextAreaElement>('textarea')?.focus({ preventScroll: true })
}

defineExpose({ getComposerRect: () => query.value?.querySelector('form')?.getBoundingClientRect() ?? query.value?.getBoundingClientRect() })
</script>

<style scoped>
.landing { --paper: #f4f2ed; --ink: #202821; --green: #254d3b; --muted: #46554b; display: block; width: 100%; margin-inline: auto; overflow: clip; color: var(--ink); background: var(--paper); font-family: var(--font-sans, inherit); -webkit-font-smoothing: antialiased; }
.landing__query-band { padding: clamp(44px, 7vw, 100px) clamp(18px, 6vw, 84px); background: #eeeee7; }
.landing__query { display: grid; max-width: 1360px; grid-template-columns: minmax(210px, .46fr) minmax(0, 1.54fr); align-items: start; gap: clamp(24px, 5vw, 78px); margin-inline: auto; scroll-margin-top: 110px; }
.landing__query-heading h2 { max-width: 360px; margin: 0; font-size: clamp(28px, 3.2vw, 43px); font-weight: 560; letter-spacing: -.035em; line-height: 1.13; text-wrap: balance; }
.landing__query-heading > p:last-child { max-width: 310px; margin: 14px 0 0; color: var(--muted); font-size: 14px; line-height: 1.75; }
.landing__paper-content { display: grid; gap: clamp(64px, 9vw, 128px); padding: clamp(58px, 8vw, 112px) clamp(18px, 6vw, 84px) 30px; }
.landing__market, .landing__paper-content :deep(.capabilities) { width: min(100%, 1360px); margin-inline: auto; }
.landing__market { display: grid; gap: 30px; }
.landing__note { display: flex; width: min(100%, 1360px); align-items: end; justify-content: space-between; gap: 18px; margin-inline: auto; padding-top: 17px; border-top: 1px solid rgb(32 40 33 / .32); }
.landing__note p { max-width: 620px; margin: 0; color: var(--muted); font-size: 12px; line-height: 1.6; }
.landing__note span { color: var(--green); font-size: 12px; letter-spacing: .04em; }
@media (max-width: 760px) { .landing__query { grid-template-columns: 1fr; gap: 24px; }.landing__query-heading > p:last-child { max-width: 520px; } }
@media (max-width: 420px) { .landing__paper-content { gap: 66px; }.landing__note { align-items: start; flex-direction: column; } }
@media (max-height: 520px) and (min-width: 761px) { .landing__query-band { padding-block: 40px; }.landing__paper-content { gap: 64px; padding-top: 56px; } }
@media (prefers-reduced-motion: reduce) { .landing { scroll-behavior: auto; } }
</style>
