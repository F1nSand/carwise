<template>
  <div class="data-table-wrap" tabindex="0" :aria-label="`${caption}数据表，可横向滚动`">
    <table>
      <caption>{{ caption }}</caption>
      <thead><tr><th v-for="column in columns" :key="column.key" scope="col">{{ column.label }}</th></tr></thead>
      <tbody>
        <tr v-for="(row, index) in rows" :key="index">
          <td v-for="column in columns" :key="column.key">{{ format(row[column.key]) }}</td>
        </tr>
        <tr v-if="!rows.length"><td :colspan="columns.length" class="empty-cell">当前条件下没有可展示的有效记录。</td></tr>
      </tbody>
    </table>
  </div>
</template>

<script setup lang="ts">
defineProps<{ caption: string; columns: Array<{ key: string; label: string; unit?: string }>; rows: Array<Record<string, unknown>> }>()
function format(value: unknown): string {
  if (value === null || value === undefined || (typeof value === 'number' && !Number.isFinite(value))) return '—'
  if (typeof value === 'number') return new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 2 }).format(value)
  if (typeof value === 'boolean') return value ? '是' : '否'
  return String(value)
}
</script>

<style scoped>
.data-table-wrap { max-width: 100%; overflow: auto; border: 1px solid var(--color-border); border-radius: 10px; }
.data-table-wrap:focus-visible { outline: 3px solid rgb(15 118 110 / .35); outline-offset: 2px; }
table { width: 100%; border-collapse: collapse; color: var(--color-ink-soft); font-size: 12px; white-space: nowrap; }
caption { padding: 10px 12px; color: var(--color-ink-muted); text-align: left; }
th, td { padding: 9px 12px; border-bottom: 1px solid var(--color-border); text-align: right; }
th:first-child, td:first-child { text-align: left; }
thead th { color: var(--color-navy); background: var(--color-surface-subtle); font-weight: 700; }
tbody tr:last-child td { border-bottom: 0; }
.empty-cell { text-align: center !important; white-space: normal; }
</style>
