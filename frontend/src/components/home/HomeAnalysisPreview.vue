<template>
  <figure class="analysis-preview" aria-labelledby="landing-title preview-caption">
    <div class="analysis-preview__image-wrap">
      <img
        class="analysis-preview__image"
        :src="heroImage"
        @error="useFallbackImage"
        alt="深森林绿色轿车停在现代建筑旁，画面作为汽车视觉示意，不代表真实挂牌车源"
        fetchpriority="high"
        decoding="async"
      />
      <div class="analysis-preview__shade" aria-hidden="true"></div>
      <div class="analysis-preview__copy">
        <h1 id="landing-title"><span class="hero-line">看清每一辆车，</span><span class="hero-line">再做选择。</span></h1>
        <p class="analysis-preview__intro">从真实历史挂牌样本出发，把预算和想法，变成有依据的判断。</p>
        <button class="analysis-preview__cta" type="button" @click="emit('jumpToComposer')">
          开始一次分析 <ArrowDownRight :size="18" aria-hidden="true" />
        </button>
      </div>
      <span class="analysis-preview__disclaimer">视觉示意 · 非真实挂牌车源</span>
    </div>
    <figcaption id="preview-caption" class="analysis-preview__caption">
      <span>以样本和统计方法，回答具体的选车问题。</span>
      <div class="analysis-preview__record" aria-live="polite">
        <span>当前数据覆盖</span>
        <strong v-if="rowCount !== null">{{ rowCount.toLocaleString('zh-CN') }}<small> 条挂牌记录</small></strong>
        <strong v-else-if="capabilitiesLoaded">暂不可读取</strong>
        <strong v-else>读取中…</strong>
      </div>
    </figcaption>
  </figure>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { ref } from 'vue'
import { ArrowDownRight } from 'lucide-vue-next'
import type { DataCapabilities } from '../../services/analysisAdapter'

const props = defineProps<{ capabilities: DataCapabilities | null; capabilitiesLoaded: boolean }>()
const emit = defineEmits<{ jumpToComposer: [] }>()
const rowCount = computed(() => {
  const value = props.capabilities?.row_count
  return typeof value === 'number' && Number.isFinite(value) ? value : null
})
const heroImage = ref('/images/automotive-atlas-hero.webp')
function useFallbackImage() {
  if (heroImage.value !== '/images/automotive-editorial.webp') heroImage.value = '/images/automotive-editorial.webp'
}
</script>

<style scoped>
.analysis-preview { display: grid; grid-template-columns: minmax(0, 1fr); min-width: 0; margin: 0; }
.analysis-preview__image-wrap { position: relative; min-height: min(690px, calc(100svh - 76px)); height: clamp(510px, 62vw, 690px); overflow: hidden; background: #17231d; isolation: isolate; }
.analysis-preview__image { position: absolute; z-index: -2; inset: 0; display: block; width: 100%; height: 100%; object-fit: cover; object-position: center 57%; }
.analysis-preview__shade { position: absolute; z-index: -1; inset: 0; background: linear-gradient(90deg, rgb(12 23 18 / .82) 0%, rgb(14 25 20 / .69) 35%, rgb(14 25 20 / .16) 69%, rgb(14 25 20 / .08) 100%), linear-gradient(0deg, rgb(12 23 18 / .42), transparent 27%); }
.analysis-preview__copy { position: absolute; inset: auto auto clamp(56px, 9vw, 118px) clamp(22px, 8vw, 120px); width: min(660px, calc(100% - 44px)); color: #fff; }
.analysis-preview__copy h1 { margin: 0; font-size: clamp(48px, 5.4vw, 78px); font-weight: 560; letter-spacing: -.03em; line-height: 1.08; }
.hero-line { display: block; white-space: nowrap; }
.analysis-preview__copy h1 span { color: #f0ede6; }
.analysis-preview__intro { max-width: 390px; margin: 23px 0 27px; color: rgb(255 255 255 / .88); font-size: clamp(14px, 1.3vw, 17px); line-height: 1.75; }
.analysis-preview__cta { display: inline-flex; min-height: 50px; align-items: center; gap: 24px; padding: 0 18px; color: #fff; border: 1px solid rgb(255 255 255 / .7); background: #254d3b; font: inherit; font-size: 14px; font-weight: 650; cursor: pointer; transition: background-color 180ms ease, border-color 180ms ease; }
.analysis-preview__cta:focus-visible { outline: 3px solid #e7b16f; outline-offset: 4px; }
.analysis-preview__disclaimer { position: absolute; bottom: 23px; color: rgb(255 255 255 / .82); font-size: 11px; }
.analysis-preview__disclaimer { left: clamp(22px, 8vw, 120px); }
.analysis-preview__caption { display: flex; min-height: 60px; align-items: center; justify-content: space-between; gap: 18px; padding: 0 clamp(18px, 6vw, 84px); color: #254d3b; border-bottom: 1px solid rgb(32 40 33 / .25); font-size: 12px; }
.analysis-preview__record { display: flex; align-items: baseline; gap: 9px; text-align: right; }
.analysis-preview__record > span { color: #46554b; }
.analysis-preview__record strong { color: #202821; font-size: 13px; font-weight: 620; font-variant-numeric: tabular-nums; }
.analysis-preview__record small { font-size: 11px; font-weight: 450; }
@media (hover: hover) and (pointer: fine) { .analysis-preview__cta:hover { border-color: #fff; background: #1b3c2d; } }
@media (max-width: 760px) { .analysis-preview__image-wrap { min-height: 0; height: min(690px, calc(100svh - 98px)); height: clamp(540px, 78svh, 700px); }.analysis-preview__image { object-position: 62% center; }.analysis-preview__shade { background: linear-gradient(90deg, rgb(12 23 18 / .72), rgb(14 25 20 / .25)), linear-gradient(0deg, rgb(12 23 18 / .56), transparent 70%); }.analysis-preview__copy { inset: auto 22px 100px; width: auto; }.analysis-preview__copy h1 { font-size: clamp(36px, 9vw, 64px); }.analysis-preview__intro { margin-block: 18px 22px; }.analysis-preview__caption { align-items: flex-start; flex-direction: column; gap: 6px; padding-block: 13px; }.analysis-preview__record { justify-content: space-between; width: 100%; text-align: left; } }
@media (max-width: 420px) { .analysis-preview__image-wrap { height: min(650px, calc(100svh - 98px)); min-height: 540px; }.analysis-preview__image { object-position: 66% center; }.analysis-preview__copy h1 { font-size: clamp(34px, 10.6vw, 44px); }.analysis-preview__copy { bottom: 93px; }.analysis-preview__disclaimer { bottom: 20px; } }
@media (max-height: 520px) and (min-width: 761px) { .analysis-preview__image-wrap { height: 420px; min-height: 0; }.analysis-preview__copy { bottom: 45px; }.analysis-preview__copy h1 { font-size: 58px; }.analysis-preview__intro { margin-block: 12px; } }
@media (max-height: 520px) and (max-width: 760px) { .analysis-preview__image-wrap { height: auto; min-height: calc(100svh - 98px); }.analysis-preview__copy { position: relative; inset: auto; width: auto; padding: 16px 18px 46px; }.analysis-preview__copy h1 { font-size: clamp(26px, 8vw, 34px); }.analysis-preview__intro { max-width: 290px; margin: 8px 0 10px; font-size: 12px; line-height: 1.5; }.analysis-preview__cta { min-height: 44px; }.analysis-preview__disclaimer { bottom: 10px; font-size: 10px; } }
@media (prefers-reduced-motion: reduce) { .analysis-preview__cta { transition: none; } }
</style>
