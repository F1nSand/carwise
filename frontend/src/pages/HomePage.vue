<template>
  <div class="home-page" :class="{ 'home-page--active': isActive }">
    <Transition name="home-context" mode="out-in">
      <section v-if="!isActive" key="home-intro" class="home-hero" aria-labelledby="home-title">
        <div class="home-hero__copy">
          <h1 id="home-title">把你的购车问题，<em>问成一份有依据的答案。</em></h1>
          <p class="home-hero__lead">说出预算、车型或你正在比较的条件。系统会从真实挂牌数据中计算行情，并把样本量、可靠性和限制一起告诉你。</p>
        </div>
        <div class="home-hero__aside" aria-label="数据摘要">
          <span class="home-hero__aside-label">当前数据覆盖</span>
          <strong>143,793</strong>
          <span>条清洗后的挂牌车源</span>
        </div>
      </section>
    </Transition>

    <section class="home-query" aria-label="开始一次分析">
      <QueryComposer v-model="text" :capabilities="dataCapabilities" :capabilities-loaded="capabilitiesLoaded" :busy="phase === 'running' || phase === 'awaiting_input'" :error="phase === 'error' && !runId ? error : ''" :error-code="phase === 'error' && !runId ? failureCode : ''" :error-request-id="phase === 'error' && !runId ? requestId : ''" :error-stage="phase === 'error' && !runId ? failureStage : ''" @submit="submitQuestion" @select-example="submitQuestion" />
    </section>

    <section v-if="phase === 'idle' || (phase === 'error' && !runId)" class="home-default-content">
      <ScenarioCards @select="selectScenario" />
      <div class="trust-strip" aria-label="系统原则">
        <div v-for="principle in principles" :key="principle.title" class="trust-strip__item">
          <span><strong>{{ principle.title }}</strong>{{ principle.description }}</span>
        </div>
      </div>
    </section>

    <section v-else-if="phase === 'reviewing' && draft" class="home-review" aria-live="polite">
      <ParameterReview :draft="draft" :capabilities="dataCapabilities" @confirm="confirmQuestion" @edit="reset" @choose-clarification="chooseClarification" />
    </section>

    <Transition name="home-context" mode="out-in">
      <section v-if="phase === 'running' || phase === 'awaiting_input' || (phase === 'error' && runId)" key="agent-workspace" class="home-workspace" aria-live="polite" aria-label="Agent 工作台">
        <p v-if="transportState === 'reconnecting'" class="workspace-connection" role="status">
          与分析服务的连接中断，正在重新连接；分析仍在后台运行。
        </p>
        <AgentRuntimePanel :runtime="runtime" :failed="phase === 'error'" />
        <section v-if="phase === 'error'" class="run-error" role="alert" data-testid="run-error">
          <div><strong>本次分析未完成</strong><p>{{ error || '运行失败，请查看计划和工具活动。' }}</p><code v-if="failureCode">{{ failureCode }}</code><small v-if="failureStage || requestId" class="run-error__diagnostic">阶段：{{ failureStage || '未知' }}<template v-if="requestId"> · 请求 ID：<code>{{ requestId }}</code></template></small></div>
          <div class="run-error__actions">
            <button v-if="retryable" class="button button--secondary" type="button" @click="retry">重试</button>
            <button class="button button--secondary" type="button" @click="reset">新建分析</button>
          </div>
        </section>
        <div class="workspace-actions">
          <AgentQuestionCard
            v-if="phase === 'awaiting_input' && runtime.pendingInput"
            class="question-card-wrap"
            :pending-input="runtime.pendingInput"
            :busy="isSubmitting"
            :submit-error="phase === 'awaiting_input' ? error : ''"
            @submit="submitRuntimeInput"
          />
          <RunCorrectionForm
            v-if="runtime.capabilities?.midrunInput"
            :busy="correctionStatus === 'submitting'"
            :status="correctionStatus"
            :error="correctionError"
            @submit="submitCorrection"
          />
          <aside v-if="!runtime.pendingInput && !runtime.capabilities?.midrunInput" class="workspace-aside" aria-label="运行提示">
            <span class="workspace-aside__pulse" aria-hidden="true"></span>
            <strong>Agent 正在自主推进</strong>
            <p>计划和工具状态会随着事件返回实时更新。这里只展示可验证的运行信息。</p>
          </aside>
        </div>
      </section>
    </Transition>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import AgentQuestionCard from '../components/analysis/AgentQuestionCard.vue'
import AgentRuntimePanel from '../components/analysis/AgentRuntimePanel.vue'
import RunCorrectionForm from '../components/analysis/RunCorrectionForm.vue'
import ParameterReview from '../components/analysis/ParameterReview.vue'
import QueryComposer from '../components/home/QueryComposer.vue'
import ScenarioCards from '../components/home/ScenarioCards.vue'
import { useAnalysisFlow } from '../composables/useAnalysisFlow'
import { getDataCapabilities, type DataCapabilities } from '../services/analysisAdapter'
import { planIsSettled } from '../utils/planState'
import type { AnalysisFilters, AnalysisIntent } from '../types/analysis'

const router = useRouter()
const route = useRoute()
const flow = useAnalysisFlow()
const dataCapabilities = ref<DataCapabilities | null>(null)
const capabilitiesLoaded = ref(false)
const { text, phase, draft, error, failureCode, requestId, failureStage, retryable, runId, runtime, transportState, isSubmitting, correctionStatus, correctionError, submit, submitInput, submitCorrection: sendCorrection, confirm, reset, retry, attachRun } = flow
const isActive = computed(() => phase.value === 'reviewing' || phase.value === 'running' || phase.value === 'awaiting_input' || (phase.value === 'error' && Boolean(runId.value)))

const principles = [
  { title: '数字有来源。', description: ' 只展示工具计算出的价格和样本。' },
  { title: '不确定性可见。', description: ' 结果会明确可靠性和数据限制。' },
  { title: '口径先说清。', description: ' 挂牌价不等于成交价，也不替代验车。' },
]

async function submitQuestion(question: string) {
  await clearResumeQuery()
  await submit(question)
  if (canOpenReport()) await router.push(`/analysis/${flow.reportId.value}`)
}

async function confirmQuestion(filters: AnalysisFilters) {
  await clearResumeQuery()
  await confirm(filters)
  if (canOpenReport()) await router.push(`/analysis/${flow.reportId.value}`)
}

async function submitRuntimeInput(answers: Parameters<typeof submitInput>[0]) {
  await submitInput(answers)
  if (canOpenReport()) await router.push(`/analysis/${flow.reportId.value}`)
}

async function submitCorrection(message: string) {
  await sendCorrection(message)
  if (canOpenReport()) await router.push(`/analysis/${flow.reportId.value}`)
}

async function clearResumeQuery() {
  if (typeof route.query.resume_run !== 'string') return
  const query = { ...route.query }
  delete query.resume_run
  await router.replace({ path: '/', query })
}

onMounted(async () => {
  void getDataCapabilities()
    .then((value) => { dataCapabilities.value = value })
    .catch(() => undefined)
    .finally(() => { capabilitiesLoaded.value = true })
  const resumeRunId = route.query.resume_run
  if (typeof resumeRunId !== 'string' || !resumeRunId) return
  await attachRun(resumeRunId)
  if (canOpenReport()) await router.replace(`/analysis/${flow.reportId.value}`)
})

function canOpenReport(): boolean {
  const plan = runtime.value.plan
  return Boolean(flow.reportId.value)
    && runtime.value.status === 'done'
    && planIsSettled(plan)
}

async function chooseClarification(value: string) {
  if (value.includes('万')) {
    await submit(`${value}以内有哪些值得比较的车系？`)
  } else {
    reset()
  }
}

function selectScenario(intent: AnalysisIntent) {
  const energy = dataCapabilities.value?.energy_values[0]
  text.value = intent === 'recommend'
    ? `15 万以内、3 年内的${energy ? `${energy}车型` : '车型'}推荐几款`
    : '2022 年的奥迪 A4L 大概什么价格'
}
</script>

<style scoped>
.home-page {
  display: grid;
  gap: clamp(20px, 3vw, 32px);
  min-width: 0;
}

.run-error { display: flex; align-items: flex-start; justify-content: space-between; gap: 20px; padding: 18px 20px; border: 1px solid rgb(185 28 28 / 18%); border-radius: 14px; background: #fff8f7; color: var(--color-ink); }
.run-error p { margin: 6px 0 8px; color: var(--color-ink-soft); line-height: 1.6; }
.run-error code { color: var(--color-danger); font-size: 12px; }
.run-error__diagnostic { display: block; margin-top: 6px; color: var(--color-ink-muted); font-size: 12px; }
.run-error__actions { display: flex; gap: 8px; flex: 0 0 auto; }
@media (max-width: 680px) { .run-error { flex-direction: column; } }

.home-hero {
  display: flex;
  align-items: end;
  justify-content: space-between;
  gap: var(--space-8);
  padding: 8px 0 var(--space-4);
}

.home-hero__copy {
  max-width: 820px;
}

h1 {
  max-width: 820px;
  margin: 0;
  color: var(--color-navy);
  font-size: clamp(36px, 4.6vw, 58px);
  font-weight: 780;
  letter-spacing: -0.03em;
  line-height: 1.05;
  text-wrap: balance;
}

h1 em {
  display: block;
  color: var(--color-teal);
  font-style: normal;
}

.home-hero__lead {
  max-width: 640px;
  margin: 16px 0 0;
  color: var(--color-ink-soft);
  font-size: 16px;
  line-height: 1.75;
}

.home-hero__aside {
  display: flex;
  align-items: baseline;
  gap: 6px;
  min-width: 150px;
}

.home-hero__aside-label,
.home-hero__aside span:last-child {
  color: var(--color-ink-muted);
  font-size: 12px;
}

.home-hero__aside strong {
  margin: 4px 0;
  color: var(--color-navy);
  font-family: var(--font-mono);
  font-size: 16px;
  letter-spacing: -0.02em;
}

.home-default-content {
  display: grid;
  gap: 0;
}

.trust-strip {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 0;
  margin-top: var(--space-8);
  border-top: 1px solid var(--color-border);
  border-bottom: 1px solid var(--color-border);
}

.trust-strip__item {
  display: flex;
  gap: 12px;
  padding: 18px 18px 18px 0;
  color: var(--color-ink-muted);
  font-size: 12px;
  line-height: 1.6;
}

.trust-strip__item + .trust-strip__item {
  padding-left: 18px;
  border-left: 1px solid var(--color-border);
}

.trust-strip strong {
  color: var(--color-navy);
  font-weight: 750;
}

.home-query {
  min-width: 0;
}

.home-workspace {
  display: grid;
  grid-template-columns: minmax(0, 1.35fr) minmax(300px, 0.65fr);
  align-items: start;
  gap: 18px;
  min-width: 0;
}

.workspace-connection {
  grid-column: 1 / -1;
  margin: 0;
  padding: 10px 14px;
  color: var(--color-ink-soft);
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-md);
  background: var(--color-surface-raised);
  font-size: 13px;
}

.home-workspace > :deep(.runtime-panel) {
  min-width: 0;
  max-width: none;
  margin: 0;
}

.home-workspace > :deep(.runtime-panel):only-child,
.home-workspace > :deep(.runtime-panel):first-child:last-child {
  grid-column: 1 / -1;
}

.workspace-actions { display: grid; align-content: start; gap: 14px; min-width: 0; }

.workspace-aside {
  display: grid;
  align-content: start;
  gap: 10px;
  min-height: 170px;
  padding: 24px;
  color: var(--color-ink-soft);
  border: 1px dashed var(--color-border-strong);
  border-radius: var(--radius-lg);
  background: rgb(255 255 255 / 0.62);
}

.workspace-aside strong {
  color: var(--color-navy);
  font-size: 14px;
}

.workspace-aside p {
  margin: 0;
  font-size: 12px;
  line-height: 1.7;
}

.workspace-aside__pulse {
  width: 9px;
  height: 9px;
  border-radius: 50%;
  background: var(--color-orange);
  box-shadow: 0 0 0 5px var(--color-orange-soft);
}

.home-page--active :deep(.composer) {
  padding: 16px 20px;
  border-radius: var(--radius-lg);
}

.home-page--active :deep(.composer__hint),
.home-page--active :deep(.composer__helper),
.home-page--active :deep(.examples) {
  display: none;
}

.home-page--active :deep(.composer textarea) {
  height: 68px;
  min-height: 68px;
  margin: 13px 0 0;
  padding: 12px 14px;
  resize: none;
}

.home-page--active :deep(.composer__topline) {
  align-items: center;
}

.home-page--active :deep(.composer__label) {
  font-size: 16px;
}

.home-context-enter-active,
.home-context-leave-active {
  transition: opacity 200ms var(--ease-standard), transform 200ms var(--ease-standard);
}

.home-context-enter-from,
.home-context-leave-to {
  opacity: 0;
  transform: translateY(8px);
}

@media (max-width: 760px) {
  .home-hero {
    align-items: flex-start;
    flex-direction: column;
    gap: 12px;
    padding-top: 0;
  }

  h1 {
    font-size: clamp(32px, 9vw, 42px);
  }

  .home-hero__aside {
    width: auto;
    min-width: 0;
    flex-wrap: wrap;
    gap: 6px;
    padding: 0;
  }

  .home-hero__aside strong {
    font-size: 15px;
  }

  .home-hero__aside span:last-child {
    flex-basis: 100%;
  }

  .home-hero__lead {
    margin-top: 12px;
    font-size: 14px;
    line-height: 1.65;
  }

  .trust-strip {
    grid-template-columns: 1fr;
  }

  .trust-strip__item,
  .trust-strip__item + .trust-strip__item {
    padding: 15px 0;
    border-left: 0;
  }

  .trust-strip__item + .trust-strip__item {
    border-top: 1px solid var(--color-border);
  }

  .home-workspace {
    grid-template-columns: 1fr;
  }

  .home-workspace > :deep(.runtime-panel),
  .home-workspace > :deep(.runtime-panel):only-child {
    grid-column: auto;
  }

  .workspace-actions { grid-column: auto; }
}

@media (max-width: 520px) {
  .home-page {
    gap: 18px;
  }

  .home-page--active :deep(.composer) {
    padding: 14px;
  }
}

@media (prefers-reduced-motion: reduce) {
  .home-context-enter-from,
  .home-context-leave-to { transform: none; }
}
</style>
