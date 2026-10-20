<template>
  <div class="markdown-content">
    <template v-for="(block, blockIndex) in blocks" :key="`${block.type}-${blockIndex}`">
      <component :is="block.type === 'heading' ? `h${block.level || 3}` : 'p'" v-if="block.type === 'heading' || block.type === 'paragraph'" class="markdown-content__paragraph" :class="{ 'markdown-content__heading': block.type === 'heading' }">
        <template v-for="(token, tokenIndex) in block.tokens || []" :key="`${token.type}-${tokenIndex}`">
          <strong v-if="token.type === 'strong'">{{ token.value }}</strong><em v-else-if="token.type === 'emphasis'">{{ token.value }}</em><code v-else-if="token.type === 'code'">{{ token.value }}</code><span v-else>{{ token.value }}</span>
        </template>
      </component>

      <ul v-else-if="block.type === 'unorderedList'" class="markdown-content__list">
        <li v-for="(item, itemIndex) in block.items || []" :key="itemIndex"><template v-for="(token, tokenIndex) in item" :key="`${token.type}-${tokenIndex}`"><strong v-if="token.type === 'strong'">{{ token.value }}</strong><em v-else-if="token.type === 'emphasis'">{{ token.value }}</em><code v-else-if="token.type === 'code'">{{ token.value }}</code><span v-else>{{ token.value }}</span></template></li>
      </ul>

      <ol v-else-if="block.type === 'orderedList'" class="markdown-content__list">
        <li v-for="(item, itemIndex) in block.items || []" :key="itemIndex"><template v-for="(token, tokenIndex) in item" :key="`${token.type}-${tokenIndex}`"><strong v-if="token.type === 'strong'">{{ token.value }}</strong><em v-else-if="token.type === 'emphasis'">{{ token.value }}</em><code v-else-if="token.type === 'code'">{{ token.value }}</code><span v-else>{{ token.value }}</span></template></li>
      </ol>

      <blockquote v-else-if="block.type === 'quote'" class="markdown-content__quote"><template v-for="(token, tokenIndex) in block.tokens || []" :key="`${token.type}-${tokenIndex}`"><strong v-if="token.type === 'strong'">{{ token.value }}</strong><em v-else-if="token.type === 'emphasis'">{{ token.value }}</em><code v-else-if="token.type === 'code'">{{ token.value }}</code><span v-else>{{ token.value }}</span></template></blockquote>

      <div v-else-if="block.type === 'table'" class="markdown-content__table-wrap">
        <table><thead><tr><th v-for="(cell, cellIndex) in block.headers || []" :key="cellIndex"><template v-for="(token, tokenIndex) in cell" :key="`${token.type}-${tokenIndex}`"><strong v-if="token.type === 'strong'">{{ token.value }}</strong><em v-else-if="token.type === 'emphasis'">{{ token.value }}</em><code v-else-if="token.type === 'code'">{{ token.value }}</code><span v-else>{{ token.value }}</span></template></th></tr></thead><tbody><tr v-for="(row, rowIndex) in block.rows || []" :key="rowIndex"><td v-for="(cell, cellIndex) in row" :key="cellIndex"><template v-for="(token, tokenIndex) in cell" :key="`${token.type}-${tokenIndex}`"><strong v-if="token.type === 'strong'">{{ token.value }}</strong><em v-else-if="token.type === 'emphasis'">{{ token.value }}</em><code v-else-if="token.type === 'code'">{{ token.value }}</code><span v-else>{{ token.value }}</span></template></td></tr></tbody></table>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { parseAgentMarkdown } from '../../utils/agentMarkdown'

const props = defineProps<{ source: string }>()
const blocks = computed(() => parseAgentMarkdown(props.source))
</script>

<style scoped>
.markdown-content { display: grid; gap: 14px; min-width: 0; color: var(--color-ink-soft); line-height: 1.8; overflow-wrap: anywhere; }
.markdown-content__paragraph { margin: 0; }
.markdown-content__heading { color: var(--color-navy); font-size: 19px; line-height: 1.35; letter-spacing: -0.035em; }
.markdown-content__list { display: grid; gap: 7px; margin: 0; padding-left: 22px; }
.markdown-content__quote { margin: 0; padding: 12px 16px; border-left: 3px solid var(--color-orange); border-radius: 0 10px 10px 0; background: var(--color-orange-soft); }
.markdown-content code { padding: 2px 5px; color: var(--color-navy); border-radius: 5px; background: rgb(15 45 70 / 0.08); font-family: var(--font-mono); font-size: 0.9em; }
.markdown-content__table-wrap { max-width: 100%; overflow-x: auto; border: 1px solid var(--color-border); border-radius: 14px; background: #fff; }
.markdown-content table { width: 100%; min-width: 460px; border-collapse: collapse; font-size: 13px; }
.markdown-content th, .markdown-content td { padding: 9px 11px; border-bottom: 1px solid var(--color-border); text-align: left; vertical-align: top; }
.markdown-content th { color: var(--color-navy); background: var(--color-teal-soft); font-weight: 750; }
.markdown-content tr:last-child td { border-bottom: 0; }
</style>
