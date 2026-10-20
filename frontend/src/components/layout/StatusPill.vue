<template>
  <span class="status-pill" :class="`status-pill--${tone}`">
    <span class="status-pill__dot" aria-hidden="true"></span>
    <span>{{ label }}</span>
  </span>
</template>

<script setup lang="ts">
withDefaults(
  defineProps<{
    tone?: 'ready' | 'warning' | 'error'
    label: string
  }>(),
  { tone: 'ready' },
)
</script>

<style scoped>
.status-pill {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  min-height: 32px;
  padding: 0 11px;
  border: 1px solid transparent;
  border-radius: 999px;
  font-size: 12px;
  font-weight: 700;
  letter-spacing: 0.01em;
  white-space: nowrap;
}

.status-pill__dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: currentColor;
}

.status-pill--ready {
  color: var(--color-teal);
  background: var(--color-teal-soft);
  border-color: rgb(15 118 110 / 0.18);
}

.status-pill--warning {
  color: var(--color-orange);
  background: var(--color-orange-soft);
  border-color: rgb(194 65 12 / 0.18);
}

.status-pill--error {
  color: var(--color-danger);
  background: var(--color-danger-soft);
  border-color: rgb(180 35 24 / 0.18);
}
</style>
