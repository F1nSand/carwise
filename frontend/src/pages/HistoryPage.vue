<template>
  <div class="history-page">
    <header class="page-heading">
      <div><h1>分析记录</h1><p>把过去问过的问题和结果留在手边，方便继续比较。</p></div>
      <RouterLink class="button button--primary" to="/"><Plus :size="16" aria-hidden="true" /> 新建分析</RouterLink>
    </header>
    <Transition name="state-fade" mode="out-in">
      <div v-if="loading" key="loading" class="state" role="status"><span class="loader" aria-hidden="true"></span><p>正在读取你的分析记录…</p></div>
      <div v-else-if="error" key="error" class="state state--error" role="alert"><AlertCircle :size="20" aria-hidden="true" /><p>{{ error }}</p><button class="button button--quiet" type="button" @click="load">重试</button></div>
      <HistoryList v-else key="records" :records="records" @open="openReport" @retry="retryRecord" @recover="recoverRecord" />
    </Transition>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { AlertCircle, Plus } from 'lucide-vue-next'
import HistoryList from '../components/history/HistoryList.vue'
import { getSessionId, listHistory, retryRun, recoverRun } from '../services/analysisAdapter'
import type { AnalysisRecord } from '../types/analysis'

const router = useRouter()
const records = ref<AnalysisRecord[]>([])
const loading = ref(true)
const error = ref('')

async function load() {
  loading.value = true
  error.value = ''
  try { records.value = await listHistory(getSessionId()) } catch (reason) { error.value = reason instanceof Error ? reason.message : '记录读取失败。' }
  finally { loading.value = false }
}

function openReport(reportId: string) { router.push(`/analysis/${reportId}`) }

async function retryRecord(runId: string) {
  loading.value = true
  error.value = ''
  try {
    const accepted = await retryRun(runId)
    await router.push({ path: '/', query: { resume_run: accepted.run_id } })
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : '重试分析失败。'
  } finally {
    loading.value = false
  }
}

onMounted(load)

async function recoverRecord(runId: string) {
  loading.value = true
  error.value = ''
  try {
    const accepted = await recoverRun(runId)
    await router.push({ path: '/', query: { resume_run: accepted.run_id } })
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : '恢复分析失败。'
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.history-page { display: grid; gap: 24px; min-width: 0; }
.page-heading { display: flex; align-items: end; justify-content: space-between; gap: 20px; }
h1 { margin: 0; color: var(--color-navy); font-size: clamp(32px, 5vw, 46px); letter-spacing: -0.055em; }
.page-heading p:last-child { margin: 12px 0 0; color: var(--color-ink-muted); font-size: 14px; }
.button { display: inline-flex; align-items: center; justify-content: center; gap: 7px; min-height: 44px; padding: 0 15px; border-radius: 10px; font-size: 13px; font-weight: 750; white-space: nowrap; }
.button--primary { color: #fff; background: var(--color-navy); }
.button--quiet { color: var(--color-navy); border: 1px solid var(--color-border); background: #fff; }
.state { display: grid; min-height: 260px; place-items: center; align-content: center; gap: 12px; padding: 24px; color: var(--color-ink-muted); font-size: 14px; text-align: center; }
.state--error { color: var(--color-danger); }
.state--error p { margin: 0; }
.loader { width: 24px; height: 24px; border: 3px solid var(--color-border); border-top-color: var(--color-teal); border-radius: 50%; animation: spin 800ms linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .loader { animation: none; } }
@media (max-width: 600px) { .page-heading { align-items: flex-start; flex-direction: column; } .page-heading .button { width: 100%; } }
</style>
