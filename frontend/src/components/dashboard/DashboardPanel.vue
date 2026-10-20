<template>
  <AnalysisChart :type="type" :title="title" :subtitle="subtitle" :result="result" :loading="loading" :error="error" @retry="emit('retry', type)" />
</template>

<script setup lang="ts">
import AnalysisChart from './AnalysisChart.vue'
import type { AnalysisResult, AnalysisType } from '../../types/dashboard'
defineProps<{ type: AnalysisType; title: string; subtitle: string; result: AnalysisResult | null; loading?: boolean; error?: string }>()
const emit = defineEmits<{ retry: [type: AnalysisType] }>()
</script>
