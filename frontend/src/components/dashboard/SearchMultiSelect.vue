<template>
  <div class="search-select">
    <label class="search-box"><Search :size="15" aria-hidden="true" /><span class="sr-only">搜索{{ label }}</span><input v-model="search" type="search" :placeholder="`搜索${label}`" :aria-label="`搜索${label}`" autocomplete="off" /></label>
    <div class="choice-list" role="group" :aria-label="label">
      <label v-for="option in filteredOptions" :key="option" class="choice">
        <input type="checkbox" :checked="model.includes(option)" :disabled="!model.includes(option) && limit !== undefined && model.length >= limit" @change="toggle(option, ($event.target as HTMLInputElement).checked)" />
        <span>{{ option }}</span>
      </label>
      <p v-if="!filteredOptions.length" class="no-options">没有匹配的{{ label }}。</p>
    </div>
    <div v-if="model.length" class="selected"><span v-for="option in model" :key="option" class="selected__item">{{ option }}<button type="button" :aria-label="`移除${option}`" @click="toggle(option, false)"><X :size="13" /></button></span></div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { Search, X } from 'lucide-vue-next'
const model = defineModel<string[]>({ required: true })
const props = defineProps<{ options: string[]; label: string; limit?: number }>()
const search = ref('')
const filteredOptions = computed(() => {
  const query = search.value.trim().toLocaleLowerCase()
  return query ? props.options.filter((option) => option.toLocaleLowerCase().includes(query)) : props.options
})
function toggle(option: string, checked: boolean) {
  if (checked) {
    if (model.value.includes(option) || (props.limit !== undefined && model.value.length >= props.limit)) return
    model.value = [...model.value, option]
  } else model.value = model.value.filter((item) => item !== option)
}
</script>

<style scoped>
.search-select { display: grid; gap: 7px; }
.search-box { display: flex; min-height: 40px; align-items: center; gap: 8px; padding: 0 10px; color: var(--color-ink-muted); border: 1px solid var(--color-border-strong); border-radius: 8px; background: #fff; }
.search-box input { min-width: 0; min-height: 38px; flex: 1; padding: 0; color: var(--color-ink); border: 0; outline: 0; background: transparent; font: inherit; font-size: 13px; }
.search-box:focus-within { outline: 3px solid rgb(15 118 110 / .25); outline-offset: 2px; border-color: var(--color-teal); }
.choice-list { display: grid; max-height: 168px; overflow: auto; padding: 4px; border: 1px solid var(--color-border); border-radius: 8px; background: #fff; }
.choice { display: flex; min-height: 38px; align-items: center; gap: 9px; padding: 4px 7px; color: var(--color-ink-soft); border-radius: 6px; font-size: 12px; cursor: pointer; }
.choice:hover { background: var(--color-surface-subtle); }
.choice input { width: 17px; height: 17px; flex: 0 0 17px; accent-color: var(--color-teal); }
.choice input:disabled { opacity: .5; }
.choice span { overflow-wrap: anywhere; }
.no-options { margin: 0; padding: 11px 8px; color: var(--color-ink-muted); font-size: 12px; }
.selected { display: flex; flex-wrap: wrap; gap: 6px; }
.selected__item { display: inline-flex; min-height: 28px; align-items: center; gap: 3px; max-width: 100%; padding: 1px 3px 1px 9px; color: var(--color-navy); border: 1px solid var(--color-border); border-radius: 999px; background: var(--color-navy-soft); font-size: 11px; overflow-wrap: anywhere; }
.selected__item button { display: grid; width: 26px; height: 26px; flex: 0 0 26px; place-items: center; color: var(--color-ink-muted); border: 0; border-radius: 50%; background: transparent; cursor: pointer; }
.selected__item button:hover { color: var(--color-danger); background: var(--color-danger-soft); }
.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }
</style>
