<template>
  <div class="about-page">
    <header class="page-heading">
      <div class="page-heading__title"><p>数据说明</p><h1>数据的范围与分析口径</h1></div>
      <p class="page-heading__intro">了解这份历史挂牌数据记录了什么，以及如何使用分析结果。以下规模与字段覆盖情况均读取自当前数据版本。</p>
    </header>
    <div class="reading-layout">
      <nav class="reading-index" aria-label="数据说明章节">
        <span>本页目录</span>
        <a href="#overview">数据概况</a>
        <a href="#coverage">字段覆盖</a>
        <a href="#filters">筛选条件</a>
        <a href="#method">分析过程</a>
        <a href="#limits">使用边界</a>
      </nav>
      <DataDisclosure class="reading-content" />
    </div>
  </div>
</template>

<script setup lang="ts">
import DataDisclosure from '../components/about/DataDisclosure.vue'
</script>

<style scoped>
.about-page { display: grid; gap: 34px; color: #202821; }
.page-heading { display: grid; grid-template-columns: minmax(0, 1.2fr) minmax(230px, .8fr); align-items: end; gap: 36px; padding: 18px 0 28px; border-bottom: 1px solid #bac2b7; }
.page-heading__title > p { margin: 0 0 16px; color: #636e64; font-size: 12px; letter-spacing: .04em; }
.page-heading__title > p span { padding: 0 5px; color: #a74b30; }
h1 { max-width: 760px; margin: 0; color: #202821; font-size: clamp(34px, 5vw, 58px); font-weight: 560; line-height: 1.08; letter-spacing: -.055em; }
.page-heading__intro { max-width: 390px; margin: 0 0 4px auto; color: #505c53; font-size: 14px; line-height: 1.75; }
.reading-layout { display: grid; grid-template-columns: 164px minmax(0, 1fr); align-items: start; gap: clamp(28px, 5vw, 72px); }
.reading-index { position: sticky; top: calc(var(--topbar-height, 76px) + 24px); display: grid; gap: 3px; padding-top: 12px; }
.reading-index > span { margin-bottom: 10px; color: #636e64; font-size: 11px; letter-spacing: .04em; }
.reading-index a { display: flex; min-height: 44px; align-items: center; border-bottom: 1px solid #dedfd7; color: #505c53; font-size: 13px; text-decoration: none; }
.reading-index a:hover { color: #254d3b; }
.reading-index a:focus-visible { outline: 2px solid #c39b50; outline-offset: 2px; }
.reading-content { min-width: 0; }
@media (max-width: 760px) {
  .about-page { gap: 24px; }
  .page-heading { grid-template-columns: 1fr; gap: 16px; padding: 8px 0 20px; }
  .page-heading__intro { margin: 0; }
  .reading-layout { grid-template-columns: 1fr; gap: 20px; }
  .reading-index { position: static; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 0 12px; padding: 0; }
  .reading-index > span { grid-column: 1 / -1; margin-bottom: 3px; }
  .reading-index a { min-height: 44px; font-size: 12px; }
}
@media (max-width: 390px) {
  h1 { font-size: 35px; }
  .reading-index { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0 10px; }
}
</style>
