<template>
  <AnalysisLanding v-if="!conversationId && route.name !== 'conversations'" ref="landing" v-model="landingDraft" :busy="sending" :error="sendError" @submit="sendMessage" />
  <div v-else class="chat-workspace" :class="{ 'chat-workspace--mobile-nav': mobileNavOpen, 'chat-workspace--entering': enteringChat }">
    <ConversationSidebar
      :items="conversations"
      :current-conversation-id="conversationId"
      :loading="sidebarLoading"
      :has-more="hasMoreConversations"
      :error="sidebarError"
      :open="mobileNavOpen"
      @new="startNewConversation"
      @select="openConversation"
      @close="mobileNavOpen = false"
      @load-more="loadMoreConversations"
    />

    <section class="chat-main" aria-label="当前会话" :class="{ 'chat-main--empty': !hasMessages, 'chat-main--active': hasMessages }">
      <header class="chat-header">
        <button class="chat-header__menu" type="button" aria-label="打开会话列表" @click="mobileNavOpen = true"><PanelLeft :size="18" /></button>
        <div class="chat-header__title"><strong>{{ currentTitle }}</strong><span>{{ headerStatus }}</span></div>
      </header>

      <ConversationPlan v-if="hasRun" class="chat-plan" :runtime="runtime" :expanded="planExpanded" @update:expanded="setPlanExpanded" />

      <section ref="thread" class="chat-thread" :class="{ 'chat-thread--empty': !hasMessages }" aria-label="对话内容" @scroll="onScroll">
        <Transition name="chat-empty"><div v-if="!hasMessages" class="chat-empty-state">
          <span class="chat-empty-state__mark" aria-hidden="true"><ChartNoAxesCombined :size="23" /></span>
          <h1>{{ conversationId ? '继续这次分析' : '把你的购车问题，问成有依据的答案。' }}</h1>
          <p>说出预算、城市或车型。Agent 会列出计划，核对挂牌数据，并说明样本与口径。</p>
          <div class="chat-empty-state__examples" aria-label="示例问题">
            <button v-for="example in examples" :key="example" type="button" @click="setDraft(example)">{{ example }}<ArrowUpRight :size="14" /></button>
          </div>
        </div></Transition>

        <div v-if="hasMessages" class="chat-timeline" role="log" aria-live="polite" aria-relevant="additions text">
          <button v-if="conversation?.hasMoreEntries" class="load-older" type="button" :disabled="loadingOlderEntries" @click="loadOlderEntries">{{ loadingOlderEntries ? '正在加载更早消息…' : '加载更早消息' }}</button>
          <template v-for="entry in visibleEntries" :key="entry.entryId">
            <MessageBubble
              v-if="entry.kind === 'user_message' || entry.kind === 'assistant_message'"
              :entry-id="entry.entryId"
              :role="entry.kind === 'user_message' ? 'user' : 'assistant'"
              :content="entryText(entry)"
              :timestamp="entry.createdAt"
              :markdown="entry.kind === 'assistant_message'"
              :delivery-state="entry.kind === 'user_message' ? String(entry.payload.delivery_status || '') : ''"
            />
            <Transition v-else-if="entry.kind === 'report_card' && reportForEntry(entry)" name="report-entry"><ReportEntryCard :report="reportForEntry(entry)!" @open="openReport" /></Transition>
            <ExecutionGroup
              v-else-if="entry.kind === 'execution_group'"
              :title="String(entry.payload.title || '本轮执行')"
              :summary="String(entry.payload.summary || '')"
              :status="entryExecutionStatus(entry)"
              :activities="executionActivities(entry)"
              :plan="executionPlan(entry)"
              :open="isExecutionEntryExpanded(entry)"
              @update:open="setExecutionEntryExpanded(entry.runId || entry.entryId, $event)"
            />
            <div v-else-if="entry.kind === 'question'" class="timeline-question"><span>{{ entryText(entry) || 'Agent 需要你确认一个条件。' }}</span><button type="button" @click="questionOpen = true">回答问题</button></div>
            <div v-else-if="entry.kind === 'run_status'" class="timeline-status">{{ entryText(entry) }}</div>
          </template>

          <ExecutionGroup
            v-if="hasRun && !hasPersistedActiveExecution"
            :open="executionExpanded"
            :status="phase === 'error' || runtime.status === 'error' ? 'error' : runtime.status === 'done' || phase === 'complete' ? 'complete' : runtime.status === 'idle' ? 'idle' : 'running'"
            :activities="runtime.activities"
            :plan="runtime.plan"
            :summary="executionSummary"
            @update:open="setExecutionExpanded"
          />

          <div v-if="runtime.pendingInput" class="question-prompt"><span class="question-prompt__dot"></span><span><strong>Agent 需要你确认一个条件</strong><small>回答后会从当前步骤继续，不会丢失这次分析。</small></span><button type="button" @click="questionOpen = true">回答</button></div>

          <section v-if="flowError && (phase === 'error' || failureCode === 'cancel_request_failed')" class="chat-error" role="alert">
            <div><strong>{{ failureCode === 'cancelled' ? '本轮已停止' : failureCode === 'cancel_request_failed' ? '停止请求未确认' : '本轮没有完成' }}</strong><p>{{ flowError }}</p><small v-if="requestId || failureStage">{{ failureStage || 'Agent' }}<template v-if="requestId"> · 请求 ID {{ requestId }}</template></small></div>
            <div class="chat-error__actions"><button v-if="retryable" type="button" @click="retryCurrent">重试</button><button v-if="activeFlow?.runId.value" type="button" @click="cancelCurrent">停止</button></div>
          </section>
        </div>
      </section>

      <Transition name="new-messages"><button v-if="hasMessages && !atBottom" class="chat-new-messages" type="button" @click="scrollToBottom">有新消息 · 回到底部 <ArrowDown :size="14" /></button></Transition>

      <div ref="composerDock" class="chat-composer-dock" :class="{ 'chat-composer-dock--centered': !hasMessages, 'chat-composer-dock--moving': composerMoving }">
        <ChatComposer v-model="composerDraft" :busy="isRunning" :disabled="sending" @submit="sendMessage" />
        <small class="chat-composer-note">Agent 仅展示可核验的数据和执行摘要，不展示隐藏思考过程。</small>
        <p v-if="sendError" class="chat-send-error" role="alert">{{ sendError }}</p>
      </div>
    </section>

    <QuestionSheet
      v-if="runtime.pendingInput && activeFlow"
      :open="questionOpen"
      :pending-input="runtime.pendingInput"
      :identity="questionIdentity"
      :draft="questionDraft"
      :busy="isSubmitting"
      :submit-error="phase === 'awaiting_input' ? flowError : ''"
      @update:open="questionOpen = $event"
      @update:draft="saveQuestionDraft"
      @submit="answerQuestion"
    />
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ArrowDown, ArrowUpRight, ChartNoAxesCombined, PanelLeft } from 'lucide-vue-next'
import ChatComposer from '../components/chat/ChatComposer.vue'
import AnalysisLanding from '../components/home/AnalysisLanding.vue'
import ConversationPlan from '../components/chat/ConversationPlan.vue'
import ConversationSidebar from '../components/chat/ConversationSidebar.vue'
import ExecutionGroup from '../components/chat/ExecutionGroup.vue'
import MessageBubble from '../components/chat/MessageBubble.vue'
import QuestionSheet from '../components/chat/QuestionSheet.vue'
import ReportEntryCard from '../components/chat/ReportEntryCard.vue'
import { retainAnalysisFlow, useAnalysisFlow } from '../composables/useAnalysisFlow'
import { conversationAdapter } from '../services/conversationAdapter'
import { createConversationStore } from '../state/conversationStore'
import type { ConversationEntry, ConversationReportRef, ConversationState, ConversationSummary } from '../types/conversation'
import type { AgentAnswer, AgentPlanStep, AgentRuntimeState, AgentToolActivity } from '../types/analysis'

const route = useRoute()
const router = useRouter()
const store = createConversationStore()
const activeFlow = shallowRef<ReturnType<typeof useAnalysisFlow> | null>(null)
let releaseActiveFlow: (() => void) | null = null
const conversations = ref<ConversationSummary[]>([])
const sidebarLoading = ref(false)
const sidebarError = ref('')
const hasMoreConversations = ref(false)
const loadingOlderEntries = ref(false)
const mobileNavOpen = ref(false)
const questionOpen = ref(false)
const sendingLanding = ref(false)
const sendingConversationIds = ref<string[]>([])
const sendError = ref('')
const landingDraft = ref('')
const atBottom = ref(true)
const composerMoving = ref(false)
const enteringChat = ref(false)
const landing = ref<InstanceType<typeof AnalysisLanding> | null>(null)
const thread = ref<HTMLElement | null>(null)
const composerDock = ref<HTMLElement | null>(null)
let lastCompletedSyncKey = ''
let suppressScrollSave = false
let scrollSaveFrame = 0
let composerAnimation: Animation | null = null
let conversationCursor: string | null = null

const conversationId = computed(() => typeof route.params.id === 'string' ? route.params.id : null)
const conversation = computed(() => conversationId.value ? store.getConversation(conversationId.value) : undefined)
const sending = computed(() => conversationId.value
  ? sendingConversationIds.value.includes(conversationId.value)
  : sendingLanding.value)
const composerDraft = computed({
  get: () => conversationId.value ? conversation.value?.composerDraft || '' : landingDraft.value,
  set: (value: string) => conversationId.value ? store.setDraft(conversationId.value, value) : landingDraft.value = value,
})
const phase = computed(() => activeFlow.value?.phase.value || 'idle')
const runtime = computed(() => activeFlow.value?.runtime.value || emptyRuntime())
const isSubmitting = computed(() => Boolean(activeFlow.value?.isSubmitting.value))
const flowError = computed(() => activeFlow.value?.error.value || '')
const failureCode = computed(() => activeFlow.value?.failureCode.value || '')
const requestId = computed(() => activeFlow.value?.requestId.value || '')
const failureStage = computed(() => activeFlow.value?.failureStage.value || '')
const retryable = computed(() => Boolean(activeFlow.value?.retryable.value))
const isRunning = computed(() => phase.value === 'running' || phase.value === 'awaiting_input')
const hasRun = computed(() => Boolean(activeFlow.value?.runId.value || conversation.value?.activeRunId))
const currentTitle = computed(() => conversation.value?.metadata.title || (conversationId.value ? '正在打开会话…' : '新会话'))
const headerStatus = computed(() => runtime.value.status === 'waiting_user'
  ? '等待你的回答'
  : isRunning.value
    ? 'Agent 正在分析'
    : conversation.value?.metadata.latestReport
      ? `最近报告 · v${conversation.value.metadata.latestReport.version}`
      : '挂牌数据 · 可核验结果')
const executionSummary = computed(() => runtime.value.status === 'waiting_user'
  ? 'Agent 正等待你回答一个问题。'
  : phase.value === 'error'
    ? '本轮未能完成，展开查看已发生的工具活动。'
    : isRunning.value
      ? runtime.value.currentStage || 'Agent 正在按计划执行。'
      : runtime.value.activities.length ? '本轮执行过程已完成。' : 'Agent 尚未开始执行。')
const questionIdentity = computed(() => {
  const pending = runtime.value.pendingInput
  if (!pending || !conversationId.value) return ''
  return `${conversationId.value}:${activeFlow.value?.runId.value || ''}:${pending.tool_call_id}:${pending.resumeRevision ?? 0}:${pending.questions.map((item) => item.id).join(',')}`
})
const questionDraft = computed(() => conversation.value?.pendingQuestionDraft || null)
const activeRunId = computed(() => activeFlow.value?.runId.value || conversation.value?.activeRunId || '')
const planClosedKey = computed(() => activeRunId.value ? `plan:${activeRunId.value}:closed` : '')
const executionKey = computed(() => activeRunId.value ? `run:${activeRunId.value}:execution` : '')
const planExpanded = computed(() => !planClosedKey.value || !conversation.value?.expandedItemIds.includes(planClosedKey.value))
const executionExpanded = computed(() => Boolean(executionKey.value && conversation.value?.expandedItemIds.includes(executionKey.value)))
const hasPersistedActiveExecution = computed(() => Boolean(activeRunId.value
  && conversation.value?.entries.some((entry) => entry.kind === 'execution_group' && entry.runId === activeRunId.value)))

const examples = [
  '15 万以内、3 年内有哪些值得比较的车？',
  '2022 年的奥迪 A4L 大概什么价格？',
  '上海和杭州的同款车挂牌价差多少？',
]

const visibleEntries = computed(() => {
  const entries = [...(conversation.value?.entries || [])]
  const report = activeFlow.value?.report.value
  if (report && !entries.some((entry) => entry.kind === 'report_card' && entry.payload.report_id === report.id)) {
    const sequence = (conversation.value?.timelineHighWaterSeq || 0) + 1
    const answer = String((report as { answer?: string }).answer || report.summaryText || '')
    entries.push({
      entryId: `local-answer:${report.id}`, conversationId: conversationId.value || '', conversationSeq: sequence,
      kind: 'assistant_message', runId: report.runId || activeFlow.value?.runId.value || null,
      sourceKey: `local-answer:${report.id}`, payload: { content: answer }, createdAt: report.generatedAt,
    })
    entries.push({
      entryId: `local-report:${report.id}`, conversationId: conversationId.value || '', conversationSeq: sequence + 1,
      kind: 'report_card', runId: report.runId || activeFlow.value?.runId.value || null,
      sourceKey: `local-report:${report.id}`, payload: { report_id: report.id, version: 1, title: report.summaryTitle, summary: report.summaryText, generated_at: report.generatedAt }, createdAt: report.generatedAt,
    })
  }
  return entries.sort((left, right) => left.conversationSeq - right.conversationSeq)
})

const hasMessages = computed(() => visibleEntries.value.length > 0 || Boolean(activeFlow.value?.runId.value) || phase.value !== 'idle')

function emptyRuntime(): AgentRuntimeState {
  return { plan: [], activities: [], pendingInput: null, waitState: null, status: 'idle', taskMode: 'understanding', taskGoals: [] } as const
}

function entryText(entry: ConversationEntry): string {
  const value = entry.payload.content ?? entry.payload.text ?? entry.payload.message ?? ''
  return typeof value === 'string' ? value : JSON.stringify(value)
}

function reportForEntry(entry: ConversationEntry): ConversationReportRef | null {
  const value = entry.payload.report
  if (value && typeof value === 'object') return value as ConversationReportRef
  const reportId = String(entry.payload.report_id || '')
  if (!reportId) return null
  return {
    reportId,
    conversationId: entry.conversationId,
    originRunId: entry.runId || String(entry.payload.origin_run_id || ''),
    version: Number(entry.payload.version || 1),
    title: String(entry.payload.title || '查看完整分析'),
    summary: String(entry.payload.summary || ''),
    generatedAt: String(entry.payload.generated_at || entry.createdAt),
  }
}

function executionActivities(entry: ConversationEntry): AgentToolActivity[] {
  const value = entry.payload.activities
  if (!Array.isArray(value)) return []
  return value.flatMap((raw): AgentToolActivity[] => {
    if (!raw || typeof raw !== 'object') return []
    const item = raw as Record<string, unknown>
    if (typeof item.id !== 'string' || typeof item.tool_name !== 'string') return []
    const status = ['running', 'waiting', 'complete', 'error'].includes(String(item.status))
      ? item.status as AgentToolActivity['status'] : 'complete'
    const activity: AgentToolActivity = {
      id: item.id,
      toolName: item.tool_name,
      status,
      detail: typeof item.detail === 'string' ? item.detail : '',
    }
    if (typeof item.plan_step_id === 'string') activity.planStepId = item.plan_step_id
    if (item.output && typeof item.output === 'object' && !Array.isArray(item.output)) {
      activity.output = item.output as Record<string, unknown>
    }
    return [activity]
  })
}

function executionPlan(entry: ConversationEntry): AgentPlanStep[] {
  const value = entry.payload.plan
  if (!Array.isArray(value)) return []
  return value.flatMap((raw): AgentPlanStep[] => {
    if (!raw || typeof raw !== 'object') return []
    const item = raw as Record<string, unknown>
    if (typeof item.id !== 'string' || typeof item.title !== 'string') return []
    const status = ['queued', 'running', 'complete', 'skipped'].includes(String(item.status))
      ? item.status as AgentPlanStep['status'] : 'queued'
    return [{
      id: item.id,
      title: item.title,
      description: typeof item.description === 'string' ? item.description : '',
      toolNames: Array.isArray(item.tool_names) ? item.tool_names.filter((name): name is string => typeof name === 'string') : [],
      goalIds: Array.isArray(item.goal_ids) ? item.goal_ids.filter((goal): goal is string => typeof goal === 'string') : [],
      kind: typeof item.kind === 'string' ? item.kind : 'data',
      completionCriteria: typeof item.completion_criteria === 'string' ? item.completion_criteria : 'tool_result',
      status,
      reason: typeof item.reason === 'string' ? item.reason : undefined,
    }]
  })
}

function entryExecutionStatus(entry: ConversationEntry): 'running' | 'complete' | 'error' | 'idle' {
  const status = entry.payload.status
  return status === 'complete' || status === 'error' || status === 'running' ? status : 'idle'
}

function executionEntryKey(runId: string): string {
  return `run:${runId}:execution`
}

function isExecutionEntryExpanded(entry: ConversationEntry): boolean {
  return Boolean(conversation.value?.expandedItemIds.includes(executionEntryKey(entry.runId || entry.entryId)))
}

function createId(): string {
  return globalThis.crypto?.randomUUID?.() || `msg-${Date.now()}-${Math.random().toString(36).slice(2)}`
}

async function refreshSidebar() {
  sidebarLoading.value = true
  sidebarError.value = ''
  try {
    const result = await conversationAdapter.listConversations(null, 50)
    conversations.value = result.items
    conversationCursor = result.nextCursor
    hasMoreConversations.value = Boolean(result.nextCursor)
  } catch {
    sidebarError.value = '会话列表暂时无法加载。'
    // Keep the chat usable when history cannot be refreshed; the current conversation is local.
  } finally {
    sidebarLoading.value = false
  }
}

async function loadMoreConversations() {
  if (sidebarLoading.value) return
  if (!conversationCursor) return refreshSidebar()
  sidebarLoading.value = true
  sidebarError.value = ''
  try {
    const result = await conversationAdapter.listConversations(conversationCursor, 50)
    const known = new Set(conversations.value.map((item) => item.conversationId))
    conversations.value = [...conversations.value, ...result.items.filter((item) => !known.has(item.conversationId))]
    conversationCursor = result.nextCursor
    hasMoreConversations.value = Boolean(result.nextCursor)
  } catch {
    sidebarError.value = '更早的会话暂时无法加载。'
  } finally {
    sidebarLoading.value = false
  }
}

async function loadOlderEntries() {
  const id = conversationId.value
  const element = thread.value
  if (!id || !element || loadingOlderEntries.value) return
  loadingOlderEntries.value = true
  const oldHeight = element.scrollHeight
  const oldTop = element.scrollTop
  suppressScrollSave = true
  try {
    await store.loadOlderEntries(id)
    await nextTick()
    if (conversationId.value === id && thread.value === element) {
      element.scrollTop = oldTop + (element.scrollHeight - oldHeight)
    }
  } catch {
    sendError.value = '更早的消息暂时无法加载，请重试。'
  } finally {
    loadingOlderEntries.value = false
    suppressScrollSave = false
  }
}

watch(conversationId, async (id) => {
  releaseActiveFlow?.()
  releaseActiveFlow = null
  composerAnimation?.cancel()
  composerAnimation = null
  composerMoving.value = false
  enteringChat.value = false
  mobileNavOpen.value = false
  sendError.value = ''
  if (!id) {
    store.dispatch({ type: 'set_active_conversation', conversationId: null })
    activeFlow.value = null
    return
  }
  suppressScrollSave = true
  const retained = retainAnalysisFlow(id)
  const flow = retained.flow
  releaseActiveFlow = retained.release
  activeFlow.value = flow
  try {
    await store.openConversation(id)
    const metadata = store.getConversation(id)?.metadata
    if (metadata?.activeRunId && ['queued', 'running', 'waiting_user'].includes(metadata.activeRunStatus || '') && flow.runId.value !== metadata.activeRunId) {
      await flow.attachRun(metadata.activeRunId)
    }
    if (typeof route.query.draft === 'string' && route.query.draft.trim()) {
      store.setDraft(id, route.query.draft)
      await nextTick()
      document.querySelector<HTMLTextAreaElement>('#chat-message')?.focus()
    }
    if (typeof route.query.source_report_id === 'string' && route.query.source_report_id.trim()) {
      store.dispatch({ type: 'set_selected_report', conversationId: id, reportId: route.query.source_report_id })
    }
    await nextTick()
    const savedAnchor = store.getConversation(id)?.scrollAnchor
    if (savedAnchor !== null && savedAnchor !== undefined && Number.isFinite(Number(savedAnchor))) {
      thread.value?.scrollTo({ top: Number(savedAnchor), behavior: 'auto' })
      onScroll()
    } else {
      scrollToBottom()
    }
  } catch (error) {
    sendError.value = error instanceof Error ? error.message : '无法打开这个会话。'
  } finally {
    suppressScrollSave = false
  }
}, { immediate: true })

watch(questionIdentity, (identity) => {
  if (!identity) {
    questionOpen.value = false
    if (conversationId.value && conversation.value?.pendingQuestionDraft) {
      store.dispatch({ type: 'set_question_draft', conversationId: conversationId.value, value: null })
    }
    return
  }
  if (questionDraft.value?.identity !== identity && conversationId.value) {
    store.dispatch({ type: 'set_question_draft', conversationId: conversationId.value, value: null })
  }
  questionOpen.value = true
})

onMounted(() => { void refreshSidebar() })
onBeforeUnmount(() => {
  composerAnimation?.cancel()
  if (scrollSaveFrame) cancelAnimationFrame(scrollSaveFrame)
  releaseActiveFlow?.()
  releaseActiveFlow = null
})

watch(() => `${conversationId.value || ''}:${activeFlow.value?.runId.value || ''}:${phase.value}`, async (key) => {
  if (!conversationId.value || phase.value !== 'complete' || key === lastCompletedSyncKey) return
  lastCompletedSyncKey = key
  await Promise.all([store.openConversation(conversationId.value), refreshSidebar()])
})

function setDraft(value: string) {
  composerDraft.value = value
  document.querySelector<HTMLTextAreaElement>('#chat-message')?.focus()
}

async function startNewConversation() {
  mobileNavOpen.value = false
  landingDraft.value = ''
  if (!conversationId.value) return
  await router.push({ name: 'conversations' })
}

async function openConversation(id: string) {
  mobileNavOpen.value = false
  if (id === conversationId.value) return
  await router.push({ name: 'conversation', params: { id } })
}

async function sendMessage(message: string) {
  if (conversationId.value
    ? sendingConversationIds.value.includes(conversationId.value)
    : sendingLanding.value) return
  const fromLanding = !conversationId.value
  const viewConversationId = conversationId.value
  const firstPosition = fromLanding ? landing.value?.getComposerRect() : !hasMessages.value ? composerDock.value?.getBoundingClientRect() : null
  if (viewConversationId) sendingConversationIds.value = [...sendingConversationIds.value, viewConversationId]
  else sendingLanding.value = true
  sendError.value = ''
  let id = conversationId.value
  let commandId = ''
  let acceptedByServer = false
  try {
    if (!id) {
      id = await store.createConversation(false)
      sendingConversationIds.value = [...sendingConversationIds.value, id]
      sendingLanding.value = false
      if (!conversationId.value) {
        store.dispatch({ type: 'set_active_conversation', conversationId: id })
        activeFlow.value = useAnalysisFlow(id)
      }
    }
    const flow = useAnalysisFlow(id)
    if (!conversationId.value || conversationId.value === id) activeFlow.value = flow
    const current = store.getConversation(id)
    const pending = current?.pendingMessageCommand
    commandId = pending?.content === message
      ? pending.commandId
      : createId()
    store.dispatch({ type: 'set_pending_message', conversationId: id, value: { content: message, commandId } })
    const seq = (current?.timelineHighWaterSeq || 0) + 1
    store.dispatch({ type: 'entry_received', entry: {
      entryId: `pending:${commandId}`, conversationId: id, conversationSeq: seq, kind: 'user_message',
      runId: flow.runId.value || null, sourceKey: `message:${commandId}`,
      payload: { text: message, client_request_id: commandId, delivery_status: 'sending' }, createdAt: new Date().toISOString(),
    } })
    if (fromLanding) {
      landingDraft.value = ''
      if (!conversationId.value) {
        await router.replace({ name: 'conversation', params: { id } })
        if (conversationId.value === id) {
          enteringChat.value = true
          window.scrollTo({ top: 0, behavior: 'instant' })
        }
      }
    }
    store.setDraft(id, '')
    await nextTick()
    if (firstPosition && composerDock.value) {
      composerMoving.value = true
      await nextTick()
      const lastPosition = composerDock.value.getBoundingClientRect()
      const dx = firstPosition.left - lastPosition.left
      const dy = firstPosition.top - lastPosition.top
      if (window.matchMedia('(prefers-reduced-motion: reduce)').matches || !composerDock.value.animate) {
        composerMoving.value = false
        enteringChat.value = false
      } else {
        const scaleX = firstPosition.width / Math.max(lastPosition.width, 1)
        const scaleY = firstPosition.height / Math.max(lastPosition.height, 1)
        const animation = composerDock.value.animate([
          { transformOrigin: 'top left', transform: `translate(${dx}px, ${dy}px) scale(${scaleX}, ${scaleY})` },
          { transformOrigin: 'top left', transform: 'translate(0, 0) scale(1, 1)' },
        ], { duration: 440, easing: 'cubic-bezier(.16, 1, .3, 1)' })
        composerAnimation = animation
        const finish = () => {
          if (composerAnimation !== animation) return
          composerAnimation = null
          composerMoving.value = false
          enteringChat.value = false
        }
        void animation.finished.then(
          finish,
          finish,
        )
      }
    }
    const sourceReportId = current?.selectedReportId || null
    const accepted = await conversationAdapter.sendMessage(id, message, commandId, sourceReportId)
    acceptedByServer = true
    store.dispatch({ type: 'entry_received', entry: accepted.entry })
    store.dispatch({ type: 'set_pending_message', conversationId: id, value: null })
    if (sourceReportId) store.dispatch({ type: 'set_selected_report', conversationId: id, reportId: null })
    if (conversationId.value === id && (typeof route.query.draft === 'string' || typeof route.query.source_report_id === 'string')) {
      await router.replace({ name: 'conversation', params: { id }, query: {} })
    }
    if (accepted.acceptedKind === 'run') {
      followRun(flow.attachRun(accepted.runId), id)
    } else if (flow.runId.value !== accepted.runId || phase.value === 'error' || phase.value === 'idle') {
      followRun(flow.attachRun(accepted.runId), id)
    } else if (phase.value === 'awaiting_input') {
      followRun(flow.resumeAcceptedRun(), id)
    }
    await refreshSidebar()
    if (conversationId.value === id) {
      await nextTick()
      if (atBottom.value) scrollToBottom()
    }
  } catch (error) {
    const isStillViewingSubmittedConversation = conversationId.value === id
    if (isStillViewingSubmittedConversation) sendError.value = error instanceof Error ? error.message : '消息发送失败。'
    if (id && commandId && !acceptedByServer) {
      const current = store.getConversation(id)
      const pendingEntry = current?.entries.find((entry) => entry.sourceKey === `message:${commandId}`)
      if (pendingEntry) store.dispatch({ type: 'entry_received', entry: {
        ...pendingEntry,
        payload: { ...pendingEntry.payload, delivery_status: 'failed' },
      } })
      store.setDraft(id, message)
    }
  } finally {
    if (id) sendingConversationIds.value = sendingConversationIds.value.filter((item) => item !== id)
    sendingLanding.value = false
    await nextTick()
    if (id && conversationId.value === id && (fromLanding || viewConversationId === id)) document.querySelector<HTMLTextAreaElement>('#chat-message')?.focus({ preventScroll: true })
  }
}

function followRun(completion: Promise<void>, id: string) {
  // The event stream can last the entire analysis; message sending ends after acceptance.
  void completion.catch((error) => {
    if (conversationId.value === id) sendError.value = error instanceof Error ? error.message : '无法继续接收分析进度。'
  })
}

async function answerQuestion(answers: AgentAnswer[]) {
  if (!activeFlow.value) return
  await activeFlow.value.submitInput(answers)
  if (activeFlow.value.phase.value === 'error') sendError.value = activeFlow.value.error.value
  if (!activeFlow.value.runtime.value.pendingInput && conversationId.value) {
    questionOpen.value = false
    store.dispatch({ type: 'set_question_draft', conversationId: conversationId.value, value: null })
  }
}

function saveQuestionDraft(value: NonNullable<ConversationState['pendingQuestionDraft']>) {
  if (!conversationId.value) return
  store.dispatch({ type: 'set_question_draft', conversationId: conversationId.value, value })
}

async function retryCurrent() {
  await activeFlow.value?.retry()
}

async function cancelCurrent() {
  await activeFlow.value?.cancel()
}

function openReport(reportId: string) {
  void router.push(`/analysis/${encodeURIComponent(reportId)}`)
}

function onScroll() {
  if (!thread.value) return
  atBottom.value = thread.value.scrollHeight - thread.value.scrollTop - thread.value.clientHeight < 44
  const id = conversationId.value
  if (!id || suppressScrollSave || scrollSaveFrame) return
  scrollSaveFrame = requestAnimationFrame(() => {
    scrollSaveFrame = 0
    if (suppressScrollSave || conversationId.value !== id || store.state.view.activeConversationId !== id || !thread.value) return
    const value = String(Math.round(thread.value.scrollTop))
    if (store.getConversation(id)?.scrollAnchor !== value) {
      store.dispatch({ type: 'set_scroll_anchor', conversationId: id, value })
    }
  })
}

function scrollToBottom() {
  if (!thread.value) return
  thread.value.scrollTo({ top: thread.value.scrollHeight, behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth' })
  atBottom.value = true
}

function setPlanExpanded(expanded: boolean) {
  const id = conversationId.value
  const itemId = planClosedKey.value
  if (!id || !itemId) return
  store.dispatch({ type: 'set_expanded_item', conversationId: id, itemId, expanded: !expanded })
}

function setExecutionExpanded(expanded: boolean) {
  const id = conversationId.value
  if (!id || !activeRunId.value) return
  setExecutionEntryExpanded(activeRunId.value, expanded)
}

function setExecutionEntryExpanded(runId: string, expanded: boolean) {
  const id = conversationId.value
  if (!id) return
  store.dispatch({ type: 'set_expanded_item', conversationId: id, itemId: executionEntryKey(runId), expanded })
}

watch(() => visibleEntries.value.length, async (count, previous) => {
  await nextTick()
  if (count > previous && atBottom.value) scrollToBottom()
})
</script>

<style scoped>
.chat-workspace { display: flex; width: 100%; height: 100%; min-height: 0; overflow: hidden; background: #fff; }
.chat-workspace--entering > :deep(.conversation-sidebar), .chat-workspace--entering .chat-header, .chat-workspace--entering .chat-thread { animation: workspace-reveal 440ms cubic-bezier(.16, 1, .3, 1) both; }
@keyframes workspace-reveal { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: translateY(0); } }
.chat-main { position: relative; display: grid; min-width: 0; min-height: 0; height: 100%; flex: 1; grid-template-rows: 58px minmax(0, auto) minmax(0, 1fr) auto; overflow: hidden; background: #fff; }
.chat-main--empty { grid-template-rows: 58px minmax(0, 1fr) auto; }
.chat-main--active { grid-template-rows: 58px minmax(0, auto) minmax(0, 1fr) auto; }
.chat-header { grid-row: 1; }
.chat-plan { grid-row: 2; min-height: 0; max-height: min(28dvh, 220px); overflow-y: auto; overscroll-behavior: contain; }
.chat-thread { grid-row: 3; }
.chat-thread--empty { grid-row: 2; }
.chat-composer-dock { grid-row: 4; }
.chat-header { z-index: 5; display: flex; align-items: center; gap: 11px; min-width: 0; padding: 0 clamp(16px, 3vw, 32px); border-bottom: 1px solid var(--color-border); background: rgb(255 255 255 / .94); }
.chat-header__title { display: grid; min-width: 0; flex: 1; gap: 2px; }
.chat-header__title strong { overflow: hidden; color: var(--color-navy); font-size: 13px; text-overflow: ellipsis; white-space: nowrap; }
.chat-header__title span, .chat-header__about { color: var(--color-ink-muted); font-size: 10px; text-decoration: none; }
.chat-header__about:hover { color: var(--color-teal); }
.chat-header__menu { display: none; width: 34px; height: 34px; place-items: center; color: var(--color-ink-soft); border: 1px solid var(--color-border); border-radius: 9px; background: #fff; }
.chat-thread { position: relative; min-height: 0; overflow-y: auto; overscroll-behavior: contain; scroll-behavior: smooth; scrollbar-color: var(--color-border-strong) transparent; scrollbar-width: thin; }
.load-older { display: block; min-height: 40px; margin: 0 auto 14px; padding: 0 13px; color: var(--color-navy); border: 1px solid var(--color-border); border-radius: 9px; background: var(--color-surface); font-size: 12px; font-weight: 650; cursor: pointer; }
.load-older:disabled { opacity: .6; cursor: wait; }
.chat-thread--empty { display: grid; align-content: safe center; padding: 20px; }
.chat-empty-state { display: grid; width: min(650px, 100%); justify-items: center; margin: 24px auto; text-align: center; }
.chat-empty-state__mark { display: grid; width: 42px; height: 42px; place-items: center; color: var(--color-teal); border: 1px solid rgb(15 118 110 / .13); border-radius: 14px; background: var(--color-teal-soft); }
.chat-empty-state h1 { max-width: 600px; margin: 17px 0 0; color: var(--color-navy); font-size: clamp(24px, 3vw, 34px); letter-spacing: -.03em; line-height: 1.22; text-wrap: balance; }
.chat-empty-state p { max-width: 525px; margin: 10px 0 0; color: var(--color-ink-muted); font-size: 13px; line-height: 1.7; }
.chat-empty-state__examples { display: flex; flex-wrap: wrap; justify-content: center; gap: 7px; margin-top: 18px; }
.chat-empty-state__examples button { display: inline-flex; align-items: center; gap: 5px; padding: 7px 9px; color: var(--color-ink-soft); border: 1px solid var(--color-border); border-radius: 999px; background: #fff; font-size: 10px; }
.chat-empty-state__examples button:hover { color: var(--color-teal); border-color: rgb(15 118 110 / .4); }
.chat-empty-leave-active { transition: opacity 140ms cubic-bezier(.3, 0, 1, 1), transform 180ms cubic-bezier(.3, 0, 1, 1); }
.chat-empty-leave-to { opacity: 0; transform: translateY(-6px) scale(.99); }
.report-entry-enter-active :deep(.report-entry) { transition: opacity 210ms cubic-bezier(.16, 1, .3, 1), transform 240ms cubic-bezier(.16, 1, .3, 1), box-shadow 240ms ease-out; }
.report-entry-enter-from :deep(.report-entry) { opacity: 0; transform: translateY(8px) scale(.99); box-shadow: 0 1px 4px rgb(15 45 70 / .02); }
.chat-timeline { max-width: 860px; min-height: 100%; margin: 0 auto; padding: 22px 0 36px; }
.chat-question { width: min(600px, calc(100% - 76px)); margin: 13px 0 18px 40px; }
.timeline-status { width: fit-content; margin: 10px auto; padding: 5px 9px; color: var(--color-ink-muted); border-radius: 999px; background: #f4f6f7; font-size: 10px; }
.chat-error { display: flex; max-width: 700px; justify-content: space-between; gap: 14px; margin: 12px 24px 20px 40px; padding: 13px; border: 1px solid rgb(185 28 28 / .17); border-radius: 11px; background: #fff8f7; }
.chat-error strong { color: var(--color-danger); font-size: 12px; }
.chat-error p { margin: 4px 0; color: var(--color-ink-soft); font-size: 11px; line-height: 1.55; }
.chat-error small { color: var(--color-ink-muted); font-size: 9px; }
.chat-error__actions { display: flex; gap: 6px; align-items: center; }
.chat-error__actions button { padding: 6px 9px; color: var(--color-navy); border: 1px solid var(--color-border); border-radius: 7px; background: #fff; font-size: 10px; white-space: nowrap; }
.chat-composer-dock { position: relative; width: min(820px, calc(100% - 44px)); margin: 0 auto; padding: 10px 0 8px; background: #fff; }
.chat-composer-dock--centered { position: relative; z-index: 3; grid-row: 3; width: min(700px, calc(100% - 40px)); margin: 0 auto clamp(8px, 4dvh, 32px); padding: 0; }
.chat-composer-dock--centered .chat-composer-note { display: none; }
.chat-composer-dock--moving { z-index: 6; will-change: transform; }
.chat-composer-note { display: block; margin-top: 7px; color: var(--color-ink-muted); font-size: 9px; text-align: center; }
.chat-send-error { margin: 4px 0 0; color: var(--color-danger); font-size: 11px; }
.chat-new-messages { position: absolute; z-index: 4; right: 50%; bottom: 108px; display: inline-flex; align-items: center; gap: 6px; padding: 7px 11px; color: #fff; border: 0; border-radius: 999px; background: var(--color-navy); box-shadow: 0 4px 15px rgb(15 45 70 / .18); font-size: 10px; transform: translateX(50%); }
.new-messages-enter-active, .new-messages-leave-active { transition: opacity 150ms ease, transform 150ms ease; }
.new-messages-enter-from, .new-messages-leave-to { opacity: 0; transform: translate(50%, 5px); }
@media (max-width: 760px) {
  .chat-header { min-height: 54px; padding: 0 13px; }
  .chat-header__menu { display: grid; }
  .chat-thread--empty { padding-right: 17px; padding-left: 17px; }
  .chat-empty-state { width: 100%; margin-block: 20px; }
  .chat-empty-state h1 { font-size: clamp(23px, 7vw, 31px); }
  .chat-empty-state__examples { display: grid; justify-items: center; }
  .chat-timeline { padding-top: 15px; }
  .chat-composer-dock { width: calc(100% - 22px); padding-bottom: max(8px, env(safe-area-inset-bottom)); }
  .chat-composer-dock--centered { width: calc(100% - 26px); }
  .chat-composer-note { font-size: 8px; }
  .chat-question { width: calc(100% - 48px); margin-left: 36px; }
  .chat-error { flex-direction: column; margin-right: 12px; margin-left: 36px; }
  .chat-error__actions { justify-content: flex-end; }
}
@media (max-height: 620px) { .chat-empty-state__examples { display: none; } }
@media (max-height: 420px) {
  .chat-thread--empty { padding-block: 8px; }
  .chat-empty-state { margin-block: 8px; }
  .chat-empty-state__mark, .chat-composer-note { display: none; }
  .chat-empty-state h1 { margin-top: 0; font-size: 22px; }
  .chat-composer-dock--centered { margin-bottom: 8px; }
}
@media (prefers-reduced-motion: reduce) { .chat-workspace--entering > :deep(.conversation-sidebar), .chat-workspace--entering .chat-header, .chat-workspace--entering .chat-thread { animation: none; } .chat-thread { scroll-behavior: auto; } .new-messages-enter-active, .new-messages-leave-active, .chat-empty-leave-active, .report-entry-enter-active :deep(.report-entry) { transition: none; } }
</style>
