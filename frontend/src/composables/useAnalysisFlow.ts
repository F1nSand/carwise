import { effectScope, getCurrentScope, onScopeDispose, ref, watch } from 'vue'
import { AnalysisAdapterError } from '../types/analysis'
import { analyzeQuestion, cancelRun, confirmAnalysis, createAnalysisContextId, getRun, getSessionId, normalizePlan, resumeAnalysis, retryRun, runtimeFromSnapshot, submitRunInput, submitRunMessage, type BackendRunSnapshot } from '../services/analysisAdapter'
import { reduceRunEvent, reduceRunSnapshot } from '../state/runReducer'
import type {
  AgentAnswer,
  AgentRuntimeState,
  AnalysisDraft,
  AnalysisFilters,
  AnalysisPhase,
  AnalysisProgressEvent,
  AnalysisRuntimeEvent,
  AnalysisReport,
  PendingInput,
  CorrectionStatus,
} from '../types/analysis'

function emptyRuntime(status = 'idle'): AgentRuntimeState {
  return {
    plan: [],
    activities: [],
    pendingInput: null,
    waitState: null,
    status,
    taskMode: 'understanding',
    taskGoals: [],
    taskIntentRevision: 0,
    capabilities: { midrunInput: false, recover: false },
    inputRevision: 0,
  }
}

const MAX_CACHED_TERMINAL_FLOWS = 16
type ConversationFlowRecord = { flow: ReturnType<typeof createAnalysisFlow>; scope: ReturnType<typeof effectScope>; activeRefs: number }
const conversationFlows = new Map<string, ConversationFlowRecord>()

function trimTerminalFlows(): void {
  let terminalCount = [...conversationFlows.values()].filter(({ flow }) => flow.phase.value !== 'running' && flow.phase.value !== 'awaiting_input').length
  if (terminalCount <= MAX_CACHED_TERMINAL_FLOWS) return
  for (const [id, record] of conversationFlows) {
    if (record.flow.phase.value === 'running' || record.flow.phase.value === 'awaiting_input') continue
    if (record.activeRefs > 0) continue
    conversationFlows.delete(id)
    record.scope.stop()
    record.flow.dispose()
    terminalCount -= 1
    if (terminalCount <= MAX_CACHED_TERMINAL_FLOWS) break
  }
}

export function useAnalysisFlow(conversationId?: string) {
  const fixedConversationId = conversationId?.trim() || ''
  if (fixedConversationId) {
    const existing = conversationFlows.get(fixedConversationId)
    if (existing) {
      conversationFlows.delete(fixedConversationId)
      conversationFlows.set(fixedConversationId, existing)
      return existing.flow
    }
    const flow = createAnalysisFlow(fixedConversationId)
    const scope = effectScope(true)
    scope.run(() => watch(flow.phase, trimTerminalFlows, { flush: 'sync' }))
    conversationFlows.set(fixedConversationId, { flow, scope, activeRefs: 0 })
    trimTerminalFlows()
    return flow
  }
  return createAnalysisFlow()
}

export function retainAnalysisFlow(conversationId: string): { flow: ReturnType<typeof useAnalysisFlow>; release: () => void } {
  const id = conversationId.trim()
  const flow = useAnalysisFlow(id)
  const record = conversationFlows.get(id)
  if (!record) return { flow, release: () => undefined }
  record.activeRefs += 1
  let released = false
  return {
    flow,
    release: () => {
      if (released) return
      released = true
      record.activeRefs = Math.max(0, record.activeRefs - 1)
      trimTerminalFlows()
    },
  }
}

function createAnalysisFlow(fixedConversationId = '') {
  const text = ref('')
  const phase = ref<AnalysisPhase>('idle')
  const draft = ref<AnalysisDraft | null>(null)
  const progress = ref<AnalysisProgressEvent[]>([])
  const runtimeEvents = ref<AnalysisRuntimeEvent[]>([])
  const runtime = ref<AgentRuntimeState>(emptyRuntime())
  const report = ref<AnalysisReport | null>(null)
  const reportId = ref('')
  const error = ref('')
  const failureCode = ref('')
  const requestId = ref('')
  const failureStage = ref('')
  const retryable = ref(false)
  const sessionId = ref(getSessionId())
  const contextId = ref(fixedConversationId)
  const runId = ref('')
  const lastEventSeq = ref(0)
  const transportState = ref<'live' | 'reconnecting' | 'offline'>('offline')
  const isSubmitting = ref(false)
  const correctionStatus = ref<CorrectionStatus>('idle')
  const correctionError = ref('')
  let submittedToolCallId = ''
  const acceptedQuestions = new Set<string>()
  let pendingSubmission: { clientRequestId: string; contextId: string; question: string; payload?: AnalysisDraft | string } | null = null
  let pendingInputCommand: { fingerprint: string; clientRequestId: string; answers: AgentAnswer[] } | null = null
  let pendingCorrection: { content: string; clientMessageId: string } | null = null
  let abortController: AbortController | null = null
  const requestControllers = new Set<AbortController>()
  let streamGeneration = 0
  let reducerAttempt: number | undefined
  let reducerStateRevision: number | undefined

  function createRequestController(): AbortController {
    const controller = new AbortController()
    requestControllers.add(controller)
    return controller
  }

  function releaseRequestController(controller: AbortController): void {
    requestControllers.delete(controller)
    if (abortController === controller) abortController = null
  }

  function disposeRequests(): void {
    streamGeneration += 1
    abortController?.abort()
    for (const controller of requestControllers) controller.abort()
    requestControllers.clear()
    abortController = null
    transportState.value = 'offline'
  }

  function applyRunAccepted(accepted: { capabilities?: { midrun_input?: boolean; recover?: boolean }; input_revision?: number }): void {
    runtime.value = {
      ...runtime.value,
      capabilities: {
        midrunInput: Boolean(accepted.capabilities?.midrun_input),
        recover: Boolean(accepted.capabilities?.recover),
      },
      inputRevision: Number.isInteger(accepted.input_revision)
        ? Math.max(runtime.value.inputRevision ?? 0, Number(accepted.input_revision))
        : runtime.value.inputRevision ?? 0,
    }
  }

  function currentReducerState() {
    return {
      runtime: runtime.value,
      progress: progress.value,
      runtimeEvents: runtimeEvents.value,
      phase: phase.value,
      correctionStatus: correctionStatus.value,
      correctionError: correctionError.value,
      isSubmitting: isSubmitting.value,
      attempt: reducerAttempt,
      stateRevision: reducerStateRevision,
      lastEventSeq: lastEventSeq.value,
    }
  }

  function commitReducerState(reduced: ReturnType<typeof currentReducerState>): void {
    runtime.value = reduced.runtime
    progress.value = reduced.progress
    runtimeEvents.value = reduced.runtimeEvents
    phase.value = reduced.phase
    correctionStatus.value = reduced.correctionStatus
    correctionError.value = reduced.correctionError
    isSubmitting.value = reduced.isSubmitting
    reducerAttempt = reduced.attempt
    reducerStateRevision = reduced.stateRevision
    lastEventSeq.value = Math.max(lastEventSeq.value, reduced.lastEventSeq ?? 0)
  }

  function onTransportState(generation: number) {
    return (state: 'live' | 'reconnecting' | 'offline') => {
      if (generation === streamGeneration) transportState.value = state
    }
  }

  function createClientMessageId(): string {
    if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') return crypto.randomUUID()
    return `message-${Date.now()}-${Math.random().toString(36).slice(2)}`
  }

  function finishWithReport(nextReport: AnalysisReport): void {
    const finalPlan = normalizePlan(nextReport.finalPlan)
    report.value = nextReport
    reportId.value = nextReport.id
    runtime.value = {
      ...runtime.value,
      plan: finalPlan.length ? finalPlan : runtime.value.plan,
      pendingInput: null,
      waitState: null,
      status: 'done',
    }
    error.value = ''
    failureCode.value = ''
    phase.value = 'complete'
  }

  function captureFailureCode(reason: unknown): void {
    failureCode.value = reason instanceof AnalysisAdapterError ? reason.failureCode || '' : ''
    retryable.value = reason instanceof AnalysisAdapterError && reason.retryable
    requestId.value = reason instanceof AnalysisAdapterError ? reason.requestId || '' : ''
    failureStage.value = reason instanceof AnalysisAdapterError ? reason.stage || '' : ''
  }

  async function submit(question: string, options: { newAnalysis?: boolean; clientRequestId?: string } = {}): Promise<void> {
    const normalized = question.replace(/\s+/g, ' ').trim()
    if (options.newAnalysis || pendingSubmission?.question !== normalized) pendingSubmission = null
    if (!pendingSubmission) pendingSubmission = {
      clientRequestId: options.clientRequestId || createClientMessageId(),
      contextId: fixedConversationId || createAnalysisContextId(),
      question: normalized,
    }
    const generation = ++streamGeneration
    abortController?.abort()
    for (const controller of requestControllers) controller.abort()
    text.value = question
    error.value = ''
    failureCode.value = ''
    retryable.value = false
    progress.value = []
    runtimeEvents.value = []
    runtime.value = emptyRuntime('running')
    report.value = null
    reportId.value = ''
    runId.value = ''
    lastEventSeq.value = 0
    reducerAttempt = undefined
    reducerStateRevision = undefined
    transportState.value = 'offline'
    correctionStatus.value = 'idle'
    correctionError.value = ''
    pendingCorrection = null

    const controller = createRequestController()
    abortController = controller
    try {
      contextId.value = pendingSubmission.contextId
      draft.value = await analyzeQuestion(question)
      if (generation !== streamGeneration) return
      await runAnalysis(question)
    } catch (reason) {
      if (generation !== streamGeneration || controller.signal.aborted) return
      phase.value = 'error'
      captureFailureCode(reason)
      error.value = reason instanceof Error ? reason.message : '暂时无法识别这条问题。'
    } finally {
      releaseRequestController(controller)
    }
  }

  async function confirm(filters?: AnalysisFilters): Promise<void> {
    if (!draft.value) return

    if (filters) draft.value = { ...draft.value, filters }
    await runAnalysis(draft.value.question, draft.value)
  }

  async function runAnalysis(question: string, submission: AnalysisDraft | string = question): Promise<void> {
    acceptedQuestions.clear()
    const generation = ++streamGeneration
    phase.value = 'running'
    error.value = ''
    failureCode.value = ''
    retryable.value = false
    progress.value = []
    runtime.value = { ...runtime.value, pendingInput: null, waitState: null, status: 'running' }
    abortController?.abort()
    const controller = createRequestController()
    abortController = controller

    if (!pendingSubmission) pendingSubmission = { clientRequestId: createClientMessageId(), contextId: contextId.value, question, payload: submission }
    pendingSubmission.payload ??= typeof submission === 'string' ? submission : JSON.parse(JSON.stringify(submission)) as AnalysisDraft
    const command = pendingSubmission
    try {
      const iterator = confirmAnalysis(command.payload!, sessionId.value, contextId.value, {
        signal: controller.signal,
        clientRequestId: command.clientRequestId,
        onTransportState: onTransportState(generation),
        onSnapshot: (snapshot) => applyBackendSnapshot(snapshot, generation),
        onRunStarted: (value) => { if (generation === streamGeneration) { runId.value = value; if (pendingSubmission === command) pendingSubmission = null } },
        onRunAccepted: (accepted) => { if (generation === streamGeneration) { applyRunAccepted(accepted); if (pendingSubmission === command) pendingSubmission = null } },
      })
      await consumeRun(iterator, generation, 'Agent 没有返回最终结果，请重试。')
    } catch (reason) {
      if (controller.signal.aborted) return
      phase.value = 'error'
      captureFailureCode(reason)
      error.value = reason instanceof Error ? reason.message : '分析过程中出现问题，请重试。'
    } finally {
      releaseRequestController(controller)
    }
  }

  function applyRuntimeEvent(event: AnalysisRuntimeEvent): void {
    if (event.kind === 'input_required' && isAcceptedQuestion(event.pendingInput, event.runId || runId.value)) {
      lastEventSeq.value = Math.max(lastEventSeq.value, event.seq || 0)
      return
    }
    const current = currentReducerState()
    const reduced = reduceRunEvent(current, event, {
      submittedToolCallId,
      pendingCorrectionClientMessageId: pendingCorrection?.clientMessageId,
      hasPendingCorrection: Boolean(pendingCorrection),
    })
    if (reduced === current) return
    const seq = 'seq' in event && Number.isInteger(event.seq) && event.seq > 0 ? event.seq : 0
    commitReducerState({ ...reduced, lastEventSeq: Math.max(lastEventSeq.value, seq) })
    // The resume cursor represents state already applied by this flow. Advancing
    // it in the transport callback can skip an event if this generation is reset
    // after receipt but before the consumer applies it.
    if ('seq' in event && Number.isInteger(event.seq) && event.seq > 0) {
      lastEventSeq.value = Math.max(lastEventSeq.value, event.seq)
    }
  }

  function applyRunSnapshot(snapshot: {
    runtime: AgentRuntimeState
    status: string
    attempt?: number
    stateRevision?: number
    lastEventSeq?: number
    runId?: string
    taskRevision?: number
    planRevision?: number
    receivedEventSeq?: number
    appliedEventSeq?: number
    coveredEventSeq?: number
    snapshotCoveredSeq?: number
    completeSnapshot?: boolean
  }): void {
    commitReducerState(reduceRunSnapshot(currentReducerState(), snapshot))
  }

  function applyBackendSnapshot(snapshot: BackendRunSnapshot, generation?: number): void {
    if (generation !== undefined && generation !== streamGeneration) return
    const hydrated = runtimeFromSnapshot(snapshot)
    if (hydrated.pendingInput && isAcceptedQuestion(hydrated.pendingInput, snapshot.run_id)) {
      hydrated.pendingInput = null
      if (snapshot.status === 'waiting_user') snapshot = { ...snapshot, status: 'running' }
    }
    const current = runtime.value
    const pendingInput = hydrated.pendingInput
    const hydratedRuntime: AgentRuntimeState = {
      ...current,
      ...hydrated,
      pendingInput: hydrated.pendingInput,
      waitState: pendingInput ? { status: 'waiting_user', toolCallId: pendingInput.tool_call_id, planStepId: pendingInput.planStepId } : null,
      status: snapshot.status,
    }
    applyRunSnapshot({
      runtime: hydratedRuntime,
      status: snapshot.status,
      attempt: snapshot.attempt,
      stateRevision: snapshot.state_revision,
      lastEventSeq: snapshot.last_event_seq,
      runId: snapshot.run_id,
      taskRevision: snapshot.task_revision ?? hydrated.taskRevision,
      planRevision: snapshot.plan_revision ?? hydrated.planRevision,
      receivedEventSeq: snapshot.received_event_seq,
      appliedEventSeq: snapshot.applied_event_seq,
      coveredEventSeq: snapshot.covered_event_seq,
      snapshotCoveredSeq: snapshot.snapshot_covered_seq,
      completeSnapshot: true,
    })
    if (snapshot.status === 'done' && snapshot.result?.report) finishWithReport(snapshot.result.report)
    if (snapshot.status === 'error' || snapshot.status === 'cancelled') {
      phase.value = 'error'
      failureCode.value = snapshot.failure_code || (snapshot.status === 'cancelled' ? 'cancelled' : 'backend_error')
      retryable.value = Boolean(snapshot.retryable)
      error.value = snapshot.error || '这次分析已结束，请重试或从检查点恢复。'
    }
  }

  async function consumeRun(
    iterator: AsyncGenerator<AnalysisRuntimeEvent, AnalysisReport | null>,
    generation: number,
    emptyResultMessage: string,
  ): Promise<void> {
    while (true) {
      const next = await iterator.next()
      if (generation !== streamGeneration) return
      if (next.done) {
        if (next.value) {
          finishWithReport(next.value)
        } else if (runtime.value.pendingInput) {
          runtime.value = { ...runtime.value, status: 'waiting_user' }
          phase.value = 'awaiting_input'
        } else {
          phase.value = 'error'
          failureCode.value = 'protocol_invariant'
          error.value = emptyResultMessage
        }
        return
      }
      applyRuntimeEvent(next.value)
    }
  }

  async function submitCorrection(content: string, clientMessageId?: string): Promise<void> {
    const message = content.trim()
    if (!message || message.length > 500 || correctionStatus.value === 'submitting') return
    if (!runId.value || !runtime.value.capabilities?.midrunInput) return
    if (phase.value !== 'running' && phase.value !== 'awaiting_input') return

    const wasWaitingForUser = phase.value === 'awaiting_input'
    let generation = streamGeneration
    let resumeController: AbortController | null = null
    const commandController = createRequestController()
    if (wasWaitingForUser) {
      generation = ++streamGeneration
      abortController?.abort()
      resumeController = commandController
      abortController = resumeController
    }

    if (!pendingCorrection || pendingCorrection.content !== message) {
      pendingCorrection = { content: message, clientMessageId: clientMessageId || createClientMessageId() }
    }
    correctionStatus.value = 'submitting'
    correctionError.value = ''
    failureCode.value = ''
    try {
      const accepted = await submitRunMessage(
        runId.value,
        message,
        pendingCorrection.clientMessageId,
        runtime.value.inputRevision ?? 0,
        commandController.signal,
      )
      if (generation !== streamGeneration) return
      runtime.value = {
        ...runtime.value,
        inputRevision: Math.max(runtime.value.inputRevision ?? 0, accepted.revision),
        ...(wasWaitingForUser ? { pendingInput: null, waitState: null, status: 'running' } : {}),
      }
      if (correctionStatus.value !== 'applied') correctionStatus.value = accepted.status === 'applied' ? 'applied' : 'received'

      if (wasWaitingForUser && resumeController && runId.value) {
        phase.value = 'running'
        error.value = ''
        const iterator = resumeAnalysis(runId.value, {
          afterSeq: lastEventSeq.value,
          signal: resumeController.signal,
          onTransportState: onTransportState(generation),
          onSnapshot: (snapshot) => applyBackendSnapshot(snapshot, generation),
        })
        await consumeRun(iterator, generation, 'Agent 收到补充条件后没有返回最终结果，请重试。')
      }
    } catch (reason) {
      if (generation !== streamGeneration || commandController.signal.aborted) return
      correctionStatus.value = 'error'
      captureFailureCode(reason)
      correctionError.value = reason instanceof Error ? reason.message : '发送补充条件失败。'
      if (wasWaitingForUser) {
        phase.value = 'awaiting_input'
      }
    } finally {
      releaseRequestController(commandController)
    }
  }

  async function attachRun(id: string): Promise<void> {
    if (id !== runId.value) acceptedQuestions.clear()
    const generation = ++streamGeneration
    abortController?.abort()
    phase.value = 'running'
    error.value = ''
    failureCode.value = ''
    report.value = null
    reportId.value = ''
    progress.value = []
    runtimeEvents.value = []
    runtime.value = emptyRuntime('running')
    runId.value = id
    correctionStatus.value = 'idle'
    correctionError.value = ''
    pendingCorrection = null
    lastEventSeq.value = 0
    reducerAttempt = undefined
    reducerStateRevision = undefined
    transportState.value = 'offline'

    let snapshotController: AbortController | null = null
    try {
      snapshotController = createRequestController()
      abortController = snapshotController
      const snapshot = await getRun(id, snapshotController.signal)
      releaseRequestController(snapshotController)
      snapshotController = null
      if (generation !== streamGeneration) return
      runId.value = snapshot.run_id
      text.value = snapshot.input
      if (fixedConversationId && snapshot.context_id && snapshot.context_id !== fixedConversationId) {
        throw new AnalysisAdapterError('该运行不属于当前会话，已阻止加载。', 'not_found')
      }
      contextId.value = fixedConversationId || snapshot.context_id || ''
      const snapshotRuntime = snapshot.runtime && typeof snapshot.runtime === 'object'
        ? snapshot.runtime as Record<string, unknown>
        : {}
      const snapshotCapabilities = snapshot.capabilities || snapshotRuntime.capabilities as { midrun_input?: boolean; recover?: boolean } | undefined
      const snapshotInputRevision = snapshot.input_revision ?? (Number.isInteger(snapshotRuntime.input_revision) ? Number(snapshotRuntime.input_revision) : 0)
      lastEventSeq.value = snapshot.last_event_seq
        ?? (Number.isInteger(snapshotRuntime.last_event_seq) ? Number(snapshotRuntime.last_event_seq) : 0)
      const hydrated = runtimeFromSnapshot(snapshot)
      const hydratedRuntime: AgentRuntimeState = {
        ...runtime.value,
        plan: hydrated.plan,
        activities: hydrated.activities,
        pendingInput: hydrated.pendingInput,
        waitState: hydrated.pendingInput ? {
          status: 'waiting_user',
          toolCallId: hydrated.pendingInput.tool_call_id,
          planStepId: hydrated.pendingInput.planStepId,
        } : null,
        planRevision: hydrated.planRevision,
        status: snapshot.status,
        capabilities: {
          midrunInput: Boolean(snapshotCapabilities?.midrun_input),
          recover: Boolean(snapshotCapabilities?.recover),
        },
        inputRevision: snapshotInputRevision,
      }
      applyRunSnapshot({
        runtime: hydratedRuntime,
        status: snapshot.status,
        attempt: snapshot.attempt,
        stateRevision: snapshot.state_revision,
        lastEventSeq: lastEventSeq.value,
        runId: snapshot.run_id,
        taskRevision: snapshot.task_revision ?? hydrated.taskRevision,
        planRevision: snapshot.plan_revision ?? hydrated.planRevision,
        receivedEventSeq: snapshot.received_event_seq,
        appliedEventSeq: snapshot.applied_event_seq,
        coveredEventSeq: snapshot.covered_event_seq,
        snapshotCoveredSeq: snapshot.snapshot_covered_seq,
      })

      if (snapshot.status === 'done') {
        const finalReport = snapshot.result?.report
        if (!finalReport) throw new Error(snapshot.result?.final_message?.content || snapshot.result?.summary || '找不到这份分析报告。')
        finishWithReport(finalReport)
        return
      }
      if (snapshot.status === 'error' || snapshot.status === 'cancelled') {
        phase.value = 'error'
        failureCode.value = snapshot.failure_code || (snapshot.status === 'cancelled' ? 'cancelled' : 'backend_error')
        retryable.value = Boolean(snapshot.retryable)
        error.value = snapshot.error || '这次分析已结束，请重试或从检查点恢复。'
        return
      }
      if (snapshot.status === 'waiting_user' && hydrated.pendingInput) {
        phase.value = 'awaiting_input'
        return
      }

      const controller = createRequestController()
      abortController = controller
      const iterator = resumeAnalysis(id, {
        afterSeq: lastEventSeq.value,
        signal: controller.signal,
        onTransportState: onTransportState(generation),
        onSnapshot: (snapshot) => applyBackendSnapshot(snapshot, generation),
      })
      await consumeRun(iterator, generation, '恢复事件流结束，但没有最终结果或待回答问题。')
      releaseRequestController(controller)
    } catch (reason) {
      if (snapshotController) releaseRequestController(snapshotController)
      if (generation !== streamGeneration || abortController?.signal.aborted) return
      phase.value = 'error'
      captureFailureCode(reason)
      error.value = reason instanceof Error ? reason.message : '恢复这次分析失败。'
      abortController = null
    }
  }

  async function resumeAcceptedRun(): Promise<void> {
    if (!runId.value) return
    const generation = ++streamGeneration
    abortController?.abort()
    phase.value = 'running'
    error.value = ''
    failureCode.value = ''
    retryable.value = false
    runtime.value = { ...runtime.value, pendingInput: null, waitState: null, status: 'running' }
    const controller = createRequestController()
    abortController = controller
    try {
      const iterator = resumeAnalysis(runId.value, {
        afterSeq: lastEventSeq.value,
        signal: controller.signal,
        onTransportState: onTransportState(generation),
        onSnapshot: (snapshot) => applyBackendSnapshot(snapshot, generation),
      })
      await consumeRun(iterator, generation, 'Agent 收到调整后没有返回最终结果，请重试。')
    } catch (reason) {
      if (generation !== streamGeneration || controller.signal.aborted) return
      phase.value = 'error'
      captureFailureCode(reason)
      error.value = reason instanceof Error ? reason.message : '恢复调整后的任务失败。'
    } finally {
      releaseRequestController(controller)
    }
  }

  async function submitInput(answers: AgentAnswer[]): Promise<void> {
    if (isSubmitting.value || !runId.value || !runtime.value.pendingInput) return
    const generation = ++streamGeneration
    isSubmitting.value = true
    const answeredToolCallId = runtime.value.pendingInput.tool_call_id
    const answeredResumeRevision = runtime.value.pendingInput.resumeRevision
    const fingerprint = JSON.stringify({ runId: runId.value, answeredToolCallId, answeredResumeRevision, answers })
    if (pendingInputCommand?.fingerprint !== fingerprint) pendingInputCommand = { fingerprint, clientRequestId: createClientMessageId(), answers: JSON.parse(JSON.stringify(answers)) as AgentAnswer[] }
    const inputCommand = pendingInputCommand
    const answeredActivities = runtime.value.activities.map((activity) => activity.id === answeredToolCallId
      ? { ...activity, status: 'running' as const, resolution: 'active' as const, detail: '正在提交回答，等待 Agent 恢复' }
      : activity)
    phase.value = 'running'
    runtime.value = {
      ...runtime.value,
      activities: answeredActivities,
      waitState: null,
      status: 'running',
    }
    error.value = ''
    failureCode.value = ''
    abortController?.abort()
    const resumeController = createRequestController()
    abortController = resumeController
    let inputAccepted = false
    try {
      submittedToolCallId = answeredToolCallId
      const accepted = await submitRunInput(
        runId.value,
        inputCommand.answers,
        answeredResumeRevision === undefined
          ? { toolCallId: answeredToolCallId }
          : { toolCallId: answeredToolCallId, resumeRevision: answeredResumeRevision },
        resumeController.signal,
        inputCommand.clientRequestId,
      )
      if (generation !== streamGeneration) return
      inputAccepted = true
      acceptedQuestions.add(JSON.stringify([runId.value, answeredToolCallId]))
      isSubmitting.value = false
      if (pendingInputCommand === inputCommand) pendingInputCommand = null
      runtime.value = { ...runtime.value, pendingInput: null, waitState: null, status: 'running' }
      const iterator = resumeAnalysis(runId.value, {
        afterSeq: lastEventSeq.value,
        answeredToolCallId: accepted?.accepted_tool_call_id || answeredToolCallId,
        signal: resumeController.signal,
        onTransportState: onTransportState(generation),
        onSnapshot: (snapshot) => applyBackendSnapshot(snapshot, generation),
      })
      await consumeRun(iterator, generation, 'Agent 恢复后没有返回最终结果，请重试。')
    } catch (reason) {
      if (generation !== streamGeneration || resumeController.signal.aborted) return
      if (!inputAccepted) {
        const restoredActivities = runtime.value.activities.map((activity) => activity.id === answeredToolCallId
          ? { ...activity, status: 'waiting' as const, resolution: 'active' as const, detail: '等待你的回答' }
          : activity)
        runtime.value = {
          ...runtime.value,
          activities: restoredActivities,
          waitState: {
            status: 'waiting_user',
            toolCallId: answeredToolCallId,
            planStepId: runtime.value.pendingInput?.planStepId,
          },
          status: 'waiting_user',
        }
        phase.value = 'awaiting_input'
      } else {
        phase.value = 'error'
      }
      captureFailureCode(reason)
      error.value = reason instanceof Error ? reason.message : '提交补充信息失败。'
    } finally {
      releaseRequestController(resumeController)
      if (generation === streamGeneration) isSubmitting.value = false
    }
  }

  function isAcceptedQuestion(pending: PendingInput, id: string): boolean {
    // A resumed worker advances attempt/revision, but the answered tool call
    // remains the same question. Keep every acceptance for this run's replay.
    return acceptedQuestions.has(JSON.stringify([id, pending.tool_call_id]))
  }

  async function retry(): Promise<void> {
    if (!retryable.value) return
    acceptedQuestions.clear()
    if (!runId.value) {
      if (text.value) await submit(text.value)
      return
    }
    const generation = ++streamGeneration
    phase.value = 'running'
    error.value = ''
    failureCode.value = ''
    retryable.value = false
    runtime.value = emptyRuntime('running')
    abortController?.abort()
    const controller = createRequestController()
    abortController = controller
    try {
      const accepted = await retryRun(runId.value, controller.signal)
      if (generation !== streamGeneration) return
      runId.value = accepted.run_id
      if (accepted.context_id) contextId.value = accepted.context_id
      applyRunAccepted(accepted)
      const iterator = resumeAnalysis(accepted.run_id, {
        signal: controller.signal,
        onTransportState: onTransportState(generation),
        onSnapshot: (snapshot) => applyBackendSnapshot(snapshot, generation),
        onRunStarted: (value) => { if (generation === streamGeneration) runId.value = value },
      })
      await consumeRun(iterator, generation, '重试后 Agent 没有返回最终结果，请重试。')
    } catch (reason) {
      if (controller.signal.aborted) return
      phase.value = 'error'
      captureFailureCode(reason)
      error.value = reason instanceof Error ? reason.message : '重试分析失败。'
    } finally {
      releaseRequestController(controller)
    }
  }

  async function resumeAfterCancel(id: string, controller: AbortController, generation: number): Promise<void> {
    if (runtime.value.status !== 'running' && runtime.value.status !== 'queued') return
    phase.value = 'running'
    const iterator = resumeAnalysis(id, {
      afterSeq: lastEventSeq.value,
      signal: controller.signal,
      onTransportState: onTransportState(generation),
      onSnapshot: (snapshot) => applyBackendSnapshot(snapshot, generation),
    })
    try {
      await consumeRun(iterator, generation, '取消请求未确认，恢复事件流也未返回最终结果。')
    } catch (resumeReason) {
      if (generation !== streamGeneration || controller.signal.aborted) return
      phase.value = 'error'
      captureFailureCode(resumeReason)
      error.value = resumeReason instanceof Error ? `取消请求失败，恢复进度也失败：${resumeReason.message}` : '取消请求失败，恢复分析进度也失败。'
    }
  }

  async function cancel(): Promise<void> {
    if (!runId.value) return
    const id = runId.value
    const generation = ++streamGeneration
    abortController?.abort()
    const controller = createRequestController()
    abortController = controller
    try {
      const snapshot = await cancelRun(runId.value, controller.signal)
      if (generation !== streamGeneration) return
      applyBackendSnapshot(snapshot, generation)
      await resumeAfterCancel(id, controller, generation)
    } catch (reason) {
      if (generation !== streamGeneration || controller.signal.aborted) return
      captureFailureCode(reason)
      failureCode.value = 'cancel_request_failed'
      error.value = reason instanceof Error ? reason.message : '取消分析失败。'
      try {
        const snapshot = await getRun(id, controller.signal)
        if (generation !== streamGeneration || controller.signal.aborted) return
        applyBackendSnapshot(snapshot, generation)
      } catch {
        // Resume below still requests an authoritative snapshot with the event stream.
      }
      if (generation !== streamGeneration || controller.signal.aborted) return
      await resumeAfterCancel(id, controller, generation)
    } finally {
      releaseRequestController(controller)
    }
  }

  function reset(): void {
    acceptedQuestions.clear()
    streamGeneration += 1
    text.value = ''
    phase.value = 'idle'
    draft.value = null
    progress.value = []
    runtimeEvents.value = []
    runtime.value = emptyRuntime()
    report.value = null
    reportId.value = ''
    error.value = ''
    failureCode.value = ''
    retryable.value = false
    runId.value = ''
    contextId.value = fixedConversationId
    lastEventSeq.value = 0
    reducerAttempt = undefined
    reducerStateRevision = undefined
    transportState.value = 'offline'
    correctionStatus.value = 'idle'
    correctionError.value = ''
    pendingCorrection = null
    pendingSubmission = null
    pendingInputCommand = null
    requestId.value = ''
    failureStage.value = ''
    disposeRequests()
    isSubmitting.value = false
  }

  // Conversation flows are long-lived: unmounting or switching the visible
  // chat only changes the view subscription, it must not cancel the run.
  if (!fixedConversationId && getCurrentScope()) onScopeDispose(disposeRequests)

  return {
    text,
    phase,
    draft,
    progress,
    runtimeEvents,
    runtime,
    report,
    reportId,
    error,
    failureCode,
    requestId,
    failureStage,
    retryable,
    sessionId,
    contextId,
    runId,
    lastEventSeq,
    transportState,
    isSubmitting,
    correctionStatus,
    correctionError,
    submit,
    confirm,
    submitInput,
    submitCorrection,
    resumeAcceptedRun,
    attachRun,
    retry,
    cancel,
    reset,
    dispose: disposeRequests,
  }
}
