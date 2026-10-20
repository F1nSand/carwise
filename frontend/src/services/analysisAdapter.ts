import {
  AnalysisAdapterError,
  type AnalysisDraft,
  type AnalysisFilters,
  type AnalysisProgressEvent,
  type AnalysisRuntimeEvent,
  type AgentAnswer,
  type AgentPlanStep,
  type AgentTaskGoal,
  type AgentToolActivity,
  type PendingInput,
  type AnalysisReport,
  type AnalysisRecord,
  type RunOutcome,
} from '../types/analysis'
import { streamRunEvents, type BackendRunEvent } from './sse'

const SESSION_STORAGE_KEY = 'used-car-intelligence.session-id'

function newClientRequestId(): string {
  return globalThis.crypto?.randomUUID?.() ?? `req-${Date.now()}-${Math.random().toString(36).slice(2)}`
}

export interface RunAccepted {
  run_id: string
  session_id: string
  context_id?: string
  status: string
  queue_position?: number | null
  attempt?: number
  retryable?: boolean
  failure_code?: string | null
  capabilities?: { midrun_input?: boolean; recover?: boolean }
  input_revision?: number
}

export interface RunInputAccepted {
  run_id: string
  status: string
  accepted_tool_call_id?: string
  resume_revision?: number
}

export interface BackendRunSnapshot {
  protocol_version?: 2
  run_id: string
  session_id: string
  input: string
  status: string
  context_id?: string
  pending_input?: PendingInput | null
  checkpoint?: Record<string, unknown> | null
  runtime?: Record<string, unknown> | null
  result?: {
    report?: AnalysisReport | null
    summary?: string
    final_message?: { content?: string }
  } | null
  error?: string | null
  request_id?: string
  stage?: string
  queue_position?: number | null
  attempt?: number
  state_revision?: number
  retryable?: boolean
  failure_code?: string | null
  recoverable?: boolean
  recovery_reason?: string
  capabilities?: { midrun_input?: boolean; recover?: boolean }
  input_revision?: number
  last_event_seq?: number
  plan_revision?: number
  task_revision?: number
  received_event_seq?: number
  applied_event_seq?: number
  covered_event_seq?: number
  snapshot_covered_seq?: number
  resume_revision?: number
}

export interface DataCapabilities {
  dataset_version: string
  row_count: number
  fields: Record<string, { coverage: number }>
  energy_values: string[]
  energy_aliases?: Record<string, string[]>
  supported_filters: string[]
  unsupported_filters: string[]
  data_time_range: { status: string; start: string | null; end: string | null }
}

export interface RuntimeSnapshotState {
  plan: AgentPlanStep[]
  activities: AgentToolActivity[]
  pendingInput: PendingInput | null
  planRevision?: number
  taskMode: 'understanding' | 'simple' | 'planned'
  taskGoals: AgentTaskGoal[]
  taskIntentRevision: number
  taskRevision?: number
  currentStage?: string
}

export interface ConfirmAnalysisOptions {
  clientRequestId?: string
  afterSeq?: number
  signal?: AbortSignal
  statusPollIntervalMs?: number
  waitForResumeTransition?: boolean
  answeredToolCallId?: string
  onRunStarted?: (runId: string) => void
  onRunAccepted?: (run: RunAccepted) => void
  onEvent?: (seq: number) => void
  onSnapshot?: (snapshot: BackendRunSnapshot) => void
  onTransportState?: (state: 'live' | 'reconnecting' | 'offline') => void
}

const apiBaseUrl = String(import.meta.env.VITE_API_BASE_URL || '').replace(/\/$/, '')

function logTerminalSource(source: 'terminal_event' | 'snapshot_watchdog' | 'stream_reconnect', runId: string): void {
  if (import.meta.env.DEV) console.debug('[analysis-adapter] terminal source', source, { runId })
}

function apiUrl(path: string): string {
  return `${apiBaseUrl}${path}`
}

function newSessionId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') return `session-${crypto.randomUUID()}`
  return `session-${Math.random().toString(36).slice(2)}-${Date.now()}`
}

export function createAnalysisContextId(): string {
  return newSessionId()
}

export function getSessionId(): string {
  if (typeof window === 'undefined') return 'session-server'
  const existing = window.localStorage.getItem(SESSION_STORAGE_KEY)
  if (existing) return existing
  const created = newSessionId()
  window.localStorage.setItem(SESSION_STORAGE_KEY, created)
  return created
}

function normalizeQuestion(text: string): string {
  return text.replace(/\s+/g, ' ').trim()
}

function wireFilters(filters: AnalysisFilters): Record<string, unknown> {
  return {
    budget_min_wan: filters.budgetMinWan ?? null,
    budget_max_wan: filters.budgetMaxWan ?? null,
    series: filters.series ?? null,
    year: filters.year ?? null,
    city: filters.city ?? null,
    energy_type: filters.energyType ?? null,
    body_type: filters.bodyType ?? null,
    max_age: filters.maxAge ?? null,
    max_mileage_wan_km: filters.maxMileageWanKm ?? null,
  }
}

async function readError(response: Response): Promise<{ message: string; failureCode?: string; retryable?: boolean; requestId?: string; stage?: string }> {
  try {
    const body = (await response.json()) as { detail?: string | { code?: string; message?: string; retryable?: boolean; request_id?: string; stage?: string; max_characters?: number }; error?: string }
    const detail = body.detail
    if (detail && typeof detail === 'object') {
      if (detail.code === 'input_too_long') {
        const limit = Number(detail.max_characters)
        return {
          message: Number.isFinite(limit) && limit > 0
            ? `输入内容超过当前上限（${limit.toLocaleString()} 个字符），请拆成几次分析。`
            : '输入内容超过当前上限，请拆成几次分析。',
          failureCode: detail.code,
          retryable: detail.retryable === true, requestId: detail.request_id, stage: detail.stage,
        }
      }
      return { message: detail.message || (detail.code ? `请求失败：${detail.code}` : `HTTP ${response.status}`), failureCode: detail.code, retryable: detail.retryable === true, requestId: detail.request_id, stage: detail.stage }
    }
    const message = detail || body.error || `HTTP ${response.status}`
    return {
      message: message === 'context_input_too_large'
        ? '提问内容与工具说明超过当前模型输入预算，请缩短问题后重试。'
        : String(message),
      failureCode: message === 'context_input_too_large' ? 'context_input_too_large' : undefined,
    }
  } catch {
    return { message: `HTTP ${response.status}` }
  }
}

async function fetchJson<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response
  try {
    response = await fetch(apiUrl(path), { ...init, credentials: 'include' })
  } catch (reason) {
    throw new AnalysisAdapterError(
      `无法连接分析服务：${reason instanceof Error ? reason.message : String(reason)}`,
      'network_error', undefined, true,
    )
  }
  if (!response.ok) {
    const error = await readError(response)
    throw new AnalysisAdapterError(error.message, 'backend_error', error.failureCode, error.retryable === true, { requestId: error.requestId || response.headers.get('X-Request-ID') || undefined, stage: error.stage, httpStatus: response.status })
  }
  try {
    return (await response.json()) as T
  } catch (reason) {
    throw new AnalysisAdapterError(
      `分析服务返回了无效数据：${reason instanceof Error ? reason.message : String(reason)}`,
      'backend_error',
    )
  }
}

export async function analyzeQuestion(text: string): Promise<AnalysisDraft> {
  const question = normalizeQuestion(text)
  if (!question) throw new AnalysisAdapterError('请输入你想分析的车型、预算或比较条件。', 'empty_input')
  // The new primary flow deliberately does not infer intent or filters in the
  // browser. The Agent decides whether it needs clarification and which tool
  // to call inside the ReAct loop. This lightweight draft remains for legacy
  // views and history fixtures only.
  return {
    id: `question-${Date.now()}`,
    question,
    intent: 'unknown',
    filters: {},
    title: 'Agent 分析中',
    scenarioLabel: 'Agent 分析',
    needsClarification: false,
  }
}

export async function getDataCapabilities(signal?: AbortSignal): Promise<DataCapabilities> {
  return fetchJson<DataCapabilities>('/v1/capabilities', { signal })
}

function legacyPhaseForEvent(event: BackendRunEvent): AnalysisProgressEvent | null {
  const eventType = event.event_type
  if (eventType === 'error') {
    const message = String(event.error || 'Agent 运行失败。')
    throw new AnalysisAdapterError(
      message,
      'backend_error',
      typeof event.failure_code === 'string' ? event.failure_code : undefined,
      event.retryable === true,
      { requestId: typeof event.request_id === 'string' ? event.request_id : undefined, stage: typeof event.stage === 'string' ? event.stage : undefined },
    )
  }
  const phase = eventType === 'tool_call'
    ? 'querying'
    : eventType === 'context'
      ? 'calculating'
      : eventType === 'final' || eventType === 'done'
        ? 'reporting'
        : 'understanding'
  const labels = {
    understanding: ['理解购车条件', 'Agent 正在整理问题和确认条件', 25],
    querying: ['查询可比车源', '正在调用受控车源分析工具', 50],
    calculating: ['计算价格与可靠性', '正在汇总工具结果和样本口径', 75],
    reporting: ['整理结论与图表', '正在整理最终 Agent 回答和报告', 100],
  } as const
  const [label, detail, percent] = labels[phase]
  return {
    phase,
    status: eventType === 'done' ? 'complete' : 'running',
    label,
    detail,
    percent,
  }
}

export function normalizePlan(raw: unknown): AgentPlanStep[] {
  if (!Array.isArray(raw)) return []
  return raw.flatMap((item) => {
    if (!item || typeof item !== 'object') return []
    const value = item as Record<string, unknown>
    return [{
      id: String(value.id || ''),
      title: String(value.title || 'Agent 计划步骤'),
      description: String(value.description || ''),
      toolNames: Array.isArray(value.tool_names) ? value.tool_names.map(String) : Array.isArray(value.toolNames) ? value.toolNames.map(String) : [],
      goalIds: Array.isArray(value.goal_ids) ? value.goal_ids.map(String) : Array.isArray(value.goalIds) ? value.goalIds.map(String) : [],
      kind: typeof value.kind === 'string' ? value.kind : undefined,
      completionCriteria: typeof value.completion_criteria === 'string'
        ? value.completion_criteria
        : typeof value.completionCriteria === 'string' ? value.completionCriteria : undefined,
      status: value.status === 'complete' || value.status === 'running' || value.status === 'skipped' ? value.status : 'queued',
      reason: typeof value.reason === 'string' ? value.reason : undefined,
    }]
  })
}

function normalizeTaskIntent(raw: unknown): {
  taskMode: 'simple' | 'planned'
  taskGoals: AgentTaskGoal[]
  taskIntentRevision: number
} | null {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null
  const value = raw as Record<string, unknown>
  if (!Array.isArray(value.goals)) return null
  const taskGoals = value.goals.flatMap((item) => {
    if (!item || typeof item !== 'object' || Array.isArray(item)) return []
    const goal = item as Record<string, unknown>
    const id = String(goal.id || '')
    if (!id) return []
    return [{
      id,
      description: String(goal.description || ''),
      deliverable: String(goal.deliverable || ''),
      dependsOn: Array.isArray(goal.depends_on) ? goal.depends_on.map(String) : [],
    }]
  }).slice(0, 8)
  if (!taskGoals.length) return null
  return {
    // New runs always have a user-visible plan. Keep reading legacy `simple`
    // values for old snapshots, but never infer the UI's direct-analysis path.
    taskMode: value.mode === 'simple' ? 'simple' : 'planned',
    taskGoals,
    taskIntentRevision: Number.isInteger(value.revision) ? Math.max(1, Number(value.revision)) : 1,
  }
}

function normalizePendingInput(event: BackendRunEvent): PendingInput | null {
  const output = (event.output && typeof event.output === 'object' ? event.output : {}) as Record<string, unknown>
  const pending = (event.pending_input && typeof event.pending_input === 'object' ? event.pending_input : {}) as Record<string, unknown>
  const questions = Array.isArray(pending.questions) ? pending.questions : Array.isArray(output.questions) ? output.questions : []
  if (!questions.length) return null
  return {
    questions: questions.flatMap((item) => {
      if (!item || typeof item !== 'object') return []
      const value = item as Record<string, unknown>
      return [{
        id: String(value.id || ''),
        prompt: String(value.prompt || ''),
        options: Array.isArray(value.options)
          ? value.options.flatMap((option) => {
              if (!option || typeof option !== 'object') return []
              const choice = option as Record<string, unknown>
              return [{ id: String(choice.id || ''), label: String(choice.label || ''), value: String(choice.value || '') }]
            })
          : [],
         allow_custom: Boolean(value.allow_custom),
         required: value.required !== false,
         selection_mode: value.selection_mode === 'multiple' ? 'multiple' as const : 'single' as const,
         max_selections: Number.isInteger(value.max_selections)
           ? Math.max(1, Math.min(6, Number(value.max_selections)))
           : value.selection_mode === 'multiple' ? 6 : 1,
      }]
    }),
    tool_call_id: String(pending.tool_call_id || event.input && (event.input as Record<string, unknown>).tool_call_id || ''),
    step: Number(pending.step || event.step || 0),
    planStepId: String(pending.plan_step_id || event.input && (event.input as Record<string, unknown>).plan_step_id || '') || undefined,
    resumeRevision: Number.isInteger(event.resume_revision)
      ? Number(event.resume_revision)
      : Number.isInteger(pending.resume_revision) ? Number(pending.resume_revision) : undefined,
  }
}

function reportFromTerminalEvent(event: BackendRunEvent): AnalysisReport | null {
  if (event.event_type !== 'done' || !event.result || typeof event.result !== 'object') return null
  const result = event.result as Record<string, unknown>
  const report = result.report
  if (!report || typeof report !== 'object' || Array.isArray(report)) return null
  return report as AnalysisReport
}

function runtimeEventForBackend(event: BackendRunEvent): AnalysisRuntimeEvent | null {
  const input = (event.input && typeof event.input === 'object' ? event.input : {}) as Record<string, unknown>
  const output = (event.output && typeof event.output === 'object' ? event.output : {}) as Record<string, unknown>
  if (event.event_type === 'message_received' || event.event_type === 'message_applied') {
    return {
      kind: event.event_type, seq: event.seq, clientMessageId: String(output.client_message_id || ''),
      revision: Number.isInteger(output.revision) ? Number(output.revision) : undefined,
    }
  }
  if (event.event_type === 'task_intent_update') {
    const intent = normalizeTaskIntent(output.task_intent)
    return intent ? {
      kind: 'task_intent_update', seq: event.seq, ...intent,
      runId: typeof event.run_id === 'string' ? event.run_id : undefined,
      attempt: Number.isInteger(event.attempt) ? Number(event.attempt) : undefined,
      stateRevision: Number.isInteger(event.state_revision) ? Number(event.state_revision) : undefined,
      taskRevision: Number.isInteger(event.task_revision) ? Number(event.task_revision) : intent.taskIntentRevision,
    } : null
  }
  if (event.event_type === 'plan_update') {
    return {
      kind: 'plan_update', seq: event.seq, plan: normalizePlan(output.steps || output.plan || event.plan),
      revision: Number.isInteger(output.revision) ? Number(output.revision) : undefined,
      reason: typeof output.reason === 'string' ? output.reason : undefined,
      runId: typeof event.run_id === 'string' ? event.run_id : undefined,
      attempt: Number.isInteger(event.attempt) ? Number(event.attempt) : undefined,
      stateRevision: Number.isInteger(event.state_revision) ? Number(event.state_revision) : undefined,
      planRevision: Number.isInteger(event.plan_revision) ? Number(event.plan_revision) : undefined,
    }
  }
  if (event.event_type === 'input_required') {
    const pendingInput = normalizePendingInput(event)
    return pendingInput ? {
      kind: 'input_required', seq: event.seq, pendingInput,
      runId: typeof event.run_id === 'string' ? event.run_id : undefined,
      attempt: Number.isInteger(event.attempt) ? Number(event.attempt) : undefined,
      stateRevision: Number.isInteger(event.state_revision) ? Number(event.state_revision) : undefined,
    } : null
  }
  if (event.event_type === 'tool_started' || event.event_type === 'tool_completed' || event.event_type === 'tool_rejected') {
    const toolName = String(input.tool_name || event.tool_name || 'Agent 工具')
      const activity: AgentToolActivity = {
      id: String(input.tool_call_id || event.tool_call_id || `${toolName}-${event.seq}`),
      toolName,
      status: event.event_type === 'tool_started' ? 'running' : event.status === 'error' || event.event_type === 'tool_rejected' ? 'error' : 'complete',
      detail: event.event_type === 'tool_started' ? '正在调用受控工具' : String(output.note || output.error || '工具已返回结果'),
      output,
        step: Number(event.step || 0),
        planStepId: String(input.plan_step_id || '') || undefined,
    }
    return {
      kind: event.event_type === 'tool_started' ? 'tool_started' : 'tool_completed', seq: event.seq, activity,
      runId: typeof event.run_id === 'string' ? event.run_id : undefined,
      attempt: Number.isInteger(event.attempt) ? Number(event.attempt) : undefined,
      stateRevision: Number.isInteger(event.state_revision) ? Number(event.state_revision) : undefined,
    }
  }
  const legacy = legacyPhaseForEvent(event)
  return legacy ? { ...legacy, kind: 'legacy_progress' } : null
}

function normalizeSnapshotActivity(raw: unknown, index: number): AgentToolActivity | null {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null
  const value = raw as Record<string, unknown>
  const eventType = String(value.event_type || '')
  if (eventType) {
    const event = runtimeEventForBackend({
      ...value,
      seq: Number.isInteger(value.seq) ? Number(value.seq) : index + 1,
      event_type: eventType,
    } as BackendRunEvent)
    return event?.kind === 'tool_started' || event?.kind === 'tool_completed' ? event.activity : null
  }
  const toolName = String(value.toolName || value.tool_name || '')
  const id = String(value.id || value.tool_call_id || '')
  if (!toolName || !id) return null
  const status = value.status === 'running' || value.status === 'waiting' || value.status === 'error' ? value.status : 'complete'
  return {
    id,
    toolName,
    planStepId: String(value.planStepId || value.plan_step_id || '') || undefined,
    status,
    detail: typeof value.detail === 'string' ? value.detail : typeof value.note === 'string' ? value.note : undefined,
    step: Number.isInteger(value.step) ? Number(value.step) : undefined,
  }
}

export function runtimeFromSnapshot(snapshot: BackendRunSnapshot): RuntimeSnapshotState {
  const runtime = snapshot.runtime && typeof snapshot.runtime === 'object'
    ? snapshot.runtime as Record<string, unknown>
    : {}
  const state = runtime.state && typeof runtime.state === 'object'
    ? runtime.state as Record<string, unknown>
    : runtime
  const rawPlan = runtime.plan || state.plan
  const rawActivities = runtime.activities || state.activities || state.run_logs
  // Checkpoints retain the suspended question while the worker resumes. It
  // is only actionable when the authoritative run status is waiting_user.
  const pending = snapshot.status === 'waiting_user'
    ? snapshot.pending_input || runtime.pending_input || state.pending_input
    : null
  const planRevision = Number.isInteger(snapshot.plan_revision)
    ? snapshot.plan_revision
    : Number.isInteger(runtime.plan_revision) ? Number(runtime.plan_revision) : undefined
  const taskRevision = Number.isInteger(snapshot.task_revision)
    ? snapshot.task_revision
    : Number.isInteger(runtime.task_revision) ? Number(runtime.task_revision) : undefined
  const normalizedPlan = normalizePlan(rawPlan)
  const taskIntent = normalizeTaskIntent(runtime.task_intent || state.task_intent)
  const currentStage = runtime.current_stage || state.current_stage
  const pendingEvent = pending && typeof pending === 'object'
    ? { seq: 0, event_type: 'input_required', pending_input: pending, resume_revision: snapshot.resume_revision } as BackendRunEvent
    : null
  return {
    plan: normalizedPlan,
    activities: Array.isArray(rawActivities)
      ? [...rawActivities.reduce((items, item, index) => {
          const activity = normalizeSnapshotActivity(item, index)
          if (activity) {
            items.delete(activity.id)
            items.set(activity.id, {
              ...activity,
              resolution: activity.status === 'error' ? 'active' : activity.status === 'running' ? 'active' : 'handled',
            })
          }
          return items
        }, new Map<string, AgentToolActivity>()).values()]
      : [],
    pendingInput: pendingEvent ? normalizePendingInput(pendingEvent) : null,
    planRevision,
    taskMode: taskIntent?.taskMode || (normalizedPlan.length ? 'planned' : 'understanding'),
    taskGoals: taskIntent?.taskGoals || [],
    taskIntentRevision: taskIntent?.taskIntentRevision || 0,
    taskRevision,
    currentStage: typeof currentStage === 'string'
      ? currentStage
      : currentStage && typeof currentStage === 'object' && typeof (currentStage as Record<string, unknown>).title === 'string'
        ? String((currentStage as Record<string, unknown>).title)
        : undefined,
  }
}

export function outcomeFromSnapshot(snapshot: BackendRunSnapshot): RunOutcome | null {
  if (snapshot.status === 'queued' || snapshot.status === 'running') return null
  if (snapshot.status === 'waiting_user') {
    const pendingInput = runtimeFromSnapshot(snapshot).pendingInput
    if (!pendingInput) throw new AnalysisAdapterError('运行正在等待用户输入，但快照中没有可回答的问题。', 'backend_error')
    return { kind: 'waiting', pendingInput, checkpoint: snapshot.checkpoint || undefined }
  }
  if (snapshot.status === 'done') {
    const report = snapshot.result?.report
    if (!report) {
      const message = snapshot.result?.final_message?.content || snapshot.result?.summary || 'Agent 没有生成结构化报告。'
      throw new AnalysisAdapterError(message, 'missing_report')
    }
    return { kind: 'completed', result: report }
  }
  if (snapshot.status === 'cancelled') {
    return { kind: 'cancelled', message: snapshot.error || '运行已取消。' }
  }
  return {
    kind: 'failed',
    failureCode: snapshot.failure_code || 'backend_error',
    message: snapshot.error || 'Agent 运行失败。',
    retryable: Boolean(snapshot.retryable),
    requestId: snapshot.request_id,
    stage: snapshot.stage,
  }
}

class EventQueue<T> {
  private items: T[] = []
  private waiters: Array<{
    resolve: (item: T | null | undefined) => void
    timer?: ReturnType<typeof setTimeout>
  }> = []
  private closed = false
  private failure: Error | null = null

  push(item: T): void {
    const waiter = this.waiters.shift()
    if (waiter) {
      if (waiter.timer) clearTimeout(waiter.timer)
      waiter.resolve(item)
    }
    else this.items.push(item)
  }

  close(): void {
    this.closed = true
    while (this.waiters.length) {
      const waiter = this.waiters.shift()
      if (waiter?.timer) clearTimeout(waiter.timer)
      waiter?.resolve(null)
    }
  }

  fail(reason: unknown): void {
    this.failure = reason instanceof Error ? reason : new Error(String(reason))
    while (this.waiters.length) {
      const waiter = this.waiters.shift()
      if (waiter?.timer) clearTimeout(waiter.timer)
      waiter?.resolve(null)
    }
  }

  async next(timeoutMs = 0): Promise<T | null | undefined> {
    if (this.items.length) return this.items.shift() ?? null
    if (this.failure) throw this.failure
    if (this.closed) return null
    return new Promise<T | null | undefined>((resolve) => {
      const waiter: {
        resolve: (item: T | null | undefined) => void
        timer?: ReturnType<typeof setTimeout>
      } = { resolve }
      this.waiters.push(waiter)
      if (timeoutMs > 0) {
        waiter.timer = setTimeout(() => {
          const index = this.waiters.indexOf(waiter)
          if (index >= 0) this.waiters.splice(index, 1)
          resolve(undefined)
        }, timeoutMs)
      }
    })
  }
}

export async function createRun(input: string, sessionId: string, contextId: string, filters: AnalysisFilters, signal?: AbortSignal, clientRequestId = newClientRequestId()): Promise<RunAccepted> {
  return fetchJson<RunAccepted>('/v1/runs', {
    method: 'POST',
    signal,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      input, session_id: sessionId, context_id: contextId,
      filters: wireFilters(filters), client_request_id: clientRequestId,
    }),
  })
}

async function* followRun(
  runId: string,
  options: ConfirmAnalysisOptions = {},
): AsyncGenerator<AnalysisRuntimeEvent, AnalysisReport | null> {
  let afterSeq = options.afterSeq ?? 0
  const seenPhases = new Set<AnalysisProgressEvent['phase']>()
  let consecutiveSnapshotFailures = 0
  const statusPollIntervalMs = Math.max(10, options.statusPollIntervalMs ?? 1000)
  const maxAttempts = options.waitForResumeTransition ? 60 : 3

  function terminalSnapshot(snapshot: BackendRunSnapshot): AnalysisReport | null | undefined {
    if (snapshot.status === 'waiting_user' && options.waitForResumeTransition) {
      const pending = runtimeFromSnapshot(snapshot).pendingInput
      if (!pending || pending.tool_call_id === options.answeredToolCallId) return undefined
    }
    const outcome = outcomeFromSnapshot(snapshot)
    if (outcome === null) return undefined
    if (outcome.kind === 'completed') return outcome.result
    if (outcome.kind === 'waiting') return null
    throw new AnalysisAdapterError(
      outcome.message,
      outcome.kind === 'failed' ? 'backend_error' : 'stream_error',
      outcome.kind === 'failed' ? outcome.failureCode : undefined,
      outcome.kind === 'failed' && outcome.retryable,
      outcome.kind === 'failed' ? { requestId: outcome.requestId, stage: outcome.stage } : {},
    )
  }

  for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
    if (options.signal?.aborted) {
      throw new AnalysisAdapterError('分析已取消。', 'stream_error')
    }
    const queue = new EventQueue<BackendRunEvent>()
    const streamController = new AbortController()
    const forwardAbort = () => streamController.abort()
    options.signal?.addEventListener('abort', forwardAbort, { once: true })
    const streamPromise = streamRunEvents(
      apiUrl(`/v1/runs/${encodeURIComponent(runId)}/events`),
      afterSeq,
      (event) => {
        options.onEvent?.(event.seq)
        queue.push(event)
      },
      streamController.signal,
      () => options.onTransportState?.('live'),
    ).then(
      (lastSeq) => {
        queue.close()
        return lastSeq
      },
      (reason) => {
        queue.fail(reason)
        throw reason
      },
    )
    const stopStream = () => {
      streamController.abort()
      options.signal?.removeEventListener('abort', forwardAbort)
      void streamPromise.catch(() => undefined)
    }

    try {
      let terminalReport: AnalysisReport | null = null
      let yieldedPendingToolCallId = ''
      while (true) {
        const event = await queue.next(statusPollIntervalMs)
        if (event === undefined) {
          if (options.signal?.aborted) {
            throw new AnalysisAdapterError('分析已取消。', 'stream_error')
          }
          try {
            const snapshot = await getRun(runId, options.signal)
            options.onSnapshot?.(snapshot)
            consecutiveSnapshotFailures = 0
            const snapshotResult = terminalSnapshot(snapshot)
            if (snapshotResult !== undefined) {
              logTerminalSource('snapshot_watchdog', runId)
              stopStream()
              options.onTransportState?.('offline')
              if (snapshotResult === null) {
                const pendingInput = runtimeFromSnapshot(snapshot).pendingInput
                if (pendingInput && pendingInput.tool_call_id !== yieldedPendingToolCallId) {
                  yield { kind: 'input_required', seq: 0, pendingInput }
                }
              }
              return snapshotResult
            }
          } catch (reason) {
            if (reason instanceof AnalysisAdapterError && reason.code === 'network_error') consecutiveSnapshotFailures += 1
            if (reason instanceof AnalysisAdapterError && (reason.code !== 'network_error' || consecutiveSnapshotFailures >= 3)) {
              stopStream()
              throw reason
            }
          }
          continue
        }
        if (!event) break
        terminalReport = terminalReport || reportFromTerminalEvent(event)
        const runtimeEvent = runtimeEventForBackend(event)
        if (runtimeEvent?.kind === 'input_required' && runtimeEvent.pendingInput.tool_call_id === options.answeredToolCallId) {
          afterSeq = Math.max(afterSeq, event.seq)
          continue
        }
        const duplicateLegacyPhase = runtimeEvent?.kind === 'legacy_progress'
          && seenPhases.has(runtimeEvent.phase)
        if (runtimeEvent && !duplicateLegacyPhase) {
          if (runtimeEvent.kind === 'legacy_progress') seenPhases.add(runtimeEvent.phase)
          if (runtimeEvent.kind === 'input_required') yieldedPendingToolCallId = runtimeEvent.pendingInput.tool_call_id
          yield runtimeEvent
          if (
            runtimeEvent.kind === 'input_required'
            && options.waitForResumeTransition
            && runtimeEvent.pendingInput.tool_call_id !== options.answeredToolCallId
          ) {
            stopStream()
            options.onTransportState?.('offline')
            return null
          }
        }
        if (terminalReport) {
          logTerminalSource('terminal_event', runId)
          stopStream()
          options.onTransportState?.('offline')
          return terminalReport
        }
      }
      afterSeq = await streamPromise
      if (terminalReport) return terminalReport
    } catch (reason) {
      options.signal?.removeEventListener('abort', forwardAbort)
      if (reason instanceof AnalysisAdapterError) {
        options.onTransportState?.('offline')
        throw reason
      }
      if (options.signal?.aborted || attempt === maxAttempts - 1) {
        if (!options.signal?.aborted) options.onTransportState?.('offline')
        throw new AnalysisAdapterError(
          `分析事件流读取失败：${reason instanceof Error ? reason.message : String(reason)}`,
          'stream_error',
        )
      }
      options.onTransportState?.('reconnecting')
      logTerminalSource('stream_reconnect', runId)
      continue
    }

    options.signal?.removeEventListener('abort', forwardAbort)
    const snapshot = await getRun(runId, options.signal)
    options.onSnapshot?.(snapshot)
    const snapshotResult = terminalSnapshot(snapshot)
    if (snapshotResult !== undefined) {
      logTerminalSource('snapshot_watchdog', runId)
      options.onTransportState?.('offline')
      if (snapshotResult === null) {
        const pendingInput = runtimeFromSnapshot(snapshot).pendingInput
        if (!pendingInput) {
          throw new AnalysisAdapterError('运行正在等待用户输入，但快照中没有可回答的问题。', 'backend_error')
        }
        yield { kind: 'input_required', seq: 0, pendingInput }
      }
      return snapshotResult
    }
    if (attempt < maxAttempts - 1) logTerminalSource('stream_reconnect', runId)
    if (attempt < maxAttempts - 1) options.onTransportState?.('reconnecting')
    if (snapshot.status === 'waiting_user' && options.waitForResumeTransition && attempt < maxAttempts - 1) {
      await new Promise((resolve) => setTimeout(resolve, statusPollIntervalMs))
    }
  }

  options.onTransportState?.('offline')
  throw new AnalysisAdapterError('分析服务连接中断，重连后仍未完成。', 'stream_error')
}

export async function* confirmAnalysis(
  draft: AnalysisDraft | string,
  sessionId = getSessionId(),
  contextId = createAnalysisContextId(),
  options: ConfirmAnalysisOptions = {},
): AsyncGenerator<AnalysisRuntimeEvent, AnalysisReport | null> {
  if (typeof draft !== 'string' && draft.needsClarification) {
    throw new AnalysisAdapterError('条件还不完整，请先补充预算。', 'invalid_draft')
  }

  const accepted = await createRun(
    typeof draft === 'string' ? draft : draft.question,
    sessionId,
    contextId,
    typeof draft === 'string' ? {} : draft.filters,
    options.signal,
    options.clientRequestId,
  )
  options.onRunStarted?.(accepted.run_id)
  options.onRunAccepted?.(accepted)
  const iterator = followRun(accepted.run_id, options)
  while (true) {
    const next = await iterator.next()
    if (next.done) return next.value
    yield next.value
  }
}

export async function submitRunInput(
  runId: string,
  answers: AgentAnswer[],
  barrier: { toolCallId?: string; resumeRevision?: number } = {},
  signal?: AbortSignal,
  clientRequestId = newClientRequestId(),
): Promise<RunInputAccepted> {
  return fetchJson<RunInputAccepted>(`/v1/runs/${encodeURIComponent(runId)}/input`, {
    method: 'POST',
    signal,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      answers,
      client_request_id: clientRequestId,
      ...(barrier.toolCallId ? { tool_call_id: barrier.toolCallId } : {}),
      ...(barrier.resumeRevision !== undefined ? { resume_revision: barrier.resumeRevision } : {}),
    }),
  })
}

export async function* resumeAnalysis(
  runId: string,
  options: ConfirmAnalysisOptions = {},
): AsyncGenerator<AnalysisRuntimeEvent, AnalysisReport | null> {
  return yield* followRun(runId, { ...options, waitForResumeTransition: true })
}

async function waitForRunCompletion(runId: string): Promise<BackendRunSnapshot> {
  let afterSeq = 0
  for (let attempt = 0; attempt < 3; attempt += 1) {
    try {
      afterSeq = await streamRunEvents(
        apiUrl(`/v1/runs/${encodeURIComponent(runId)}/events`),
        afterSeq,
        () => undefined,
      )
    } catch (reason) {
      if (attempt === 2) {
        throw new AnalysisAdapterError(
          `报告事件流读取失败：${reason instanceof Error ? reason.message : String(reason)}`,
          'stream_error',
        )
      }
      continue
    }
    const snapshot = await getRun(runId)
    if (snapshot.status === 'done' || snapshot.status === 'error' || snapshot.status === 'cancelled') return snapshot
  }
  throw new AnalysisAdapterError('报告事件流结束后运行仍未完成。', 'stream_error')
}

export async function getRun(runId: string, signal?: AbortSignal): Promise<BackendRunSnapshot> {
  for (let attempt = 0; ; attempt += 1) {
    try {
      return await fetchJson<BackendRunSnapshot>(`/v1/runs/${encodeURIComponent(runId)}`, { signal })
    } catch (reason) {
      const transient = reason instanceof AnalysisAdapterError && (reason.code === 'network_error' || (reason.httpStatus === 503 && reason.retryable))
      if (signal?.aborted || !transient || attempt >= 2) throw reason
      await new Promise<void>((resolve) => {
        const finish = () => { clearTimeout(timer); signal?.removeEventListener('abort', finish); resolve() }
        const timer = setTimeout(finish, 100 * (2 ** attempt))
        signal?.addEventListener('abort', finish, { once: true })
      })
    }
  }
}

export async function getReport(id: string): Promise<AnalysisReport> {
  try {
    const artifact = await fetchJson<{
      report_id: string
      conversation_id: string
      origin_run_id: string
      version: number
      parent_report_id?: string | null
      content?: AnalysisReport
      created_at?: string
    }>(`/v1/reports/${encodeURIComponent(id)}`)
    if (!artifact.content || typeof artifact.content !== 'object') {
      throw new AnalysisAdapterError('报告存储记录缺少正文。', 'missing_report')
    }
    return {
      ...artifact.content,
      id: artifact.content.id || artifact.report_id,
      runId: artifact.origin_run_id,
      conversationId: artifact.conversation_id,
      reportVersion: artifact.version,
      parentReportId: artifact.parent_report_id || null,
      generatedAt: artifact.content.generatedAt || artifact.created_at || '',
    }
  } catch (reason) {
    const legacyArtifactRead = reason instanceof AnalysisAdapterError
      && (reason.httpStatus === 404 || reason.httpStatus === 501
        || reason.failureCode === 'report_artifact_storage_unavailable')
    if (!legacyArtifactRead) throw reason
  }

  let snapshot = await getRun(id)
  if (snapshot.status === 'queued' || snapshot.status === 'running') {
    snapshot = await waitForRunCompletion(id)
  }
  if (snapshot.status === 'error') throw new AnalysisAdapterError(snapshot.error || '报告运行失败。', 'backend_error', snapshot.failure_code || undefined, Boolean(snapshot.retryable), { requestId: snapshot.request_id, stage: snapshot.stage })
  if (!snapshot.result?.report) {
    throw new AnalysisAdapterError(
      snapshot.result?.final_message?.content || snapshot.result?.summary || '找不到这份分析报告，请重新发起分析。',
      'missing_report',
    )
  }
  const report = snapshot.result.report
  const artifactIdentity = {
    ...(snapshot.context_id ? { conversationId: snapshot.context_id } : {}),
    runId: snapshot.run_id,
    reportVersion: 1,
    parentReportId: null,
  }
  if (report.finalPlan?.length) return { ...report, ...artifactIdentity }
  const durablePlan = runtimeFromSnapshot(snapshot).plan
  return durablePlan.length
    ? { ...report, ...artifactIdentity, finalPlan: durablePlan, attempt: snapshot.attempt }
    : { ...report, ...artifactIdentity }
}

export async function listHistory(sessionId = getSessionId()): Promise<AnalysisRecord[]> {
  const records = await fetchJson<Array<AnalysisRecord & { failure_code?: string; recovery_reason?: string }>>(`/v1/history?session_id=${encodeURIComponent(sessionId)}`)
  return records.map((record) => ({
    ...record,
    retryable: record.retryable === true,
    failureCode: record.failureCode ?? record.failure_code,
    recoveryReason: record.recoveryReason ?? record.recovery_reason,
  }))
}

// Retain unacknowledged retry/recovery command identity across UI remounts.
// An acknowledged command is released; a later explicit action is a new command.
const pendingRunCommands = new Map<string, string>()
async function sendRunCommand(kind: 'retry' | 'recover', runId: string, signal?: AbortSignal, clientRequestId?: string): Promise<RunAccepted> {
  const command = `${kind}:${runId}`
  const identity = clientRequestId || pendingRunCommands.get(command) || newClientRequestId()
  pendingRunCommands.set(command, identity)
  const accepted = await fetchJson<RunAccepted>(`/v1/runs/${encodeURIComponent(runId)}/${kind}`, {
    method: 'POST', signal, headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ client_request_id: identity }),
  })
  if (pendingRunCommands.get(command) === identity) pendingRunCommands.delete(command)
  return accepted
}
export async function retryRun(runId: string, signal?: AbortSignal, clientRequestId?: string): Promise<RunAccepted> {
  return sendRunCommand('retry', runId, signal, clientRequestId)
}
export async function recoverRun(runId: string, signal?: AbortSignal, clientRequestId?: string): Promise<RunAccepted> {
  return sendRunCommand('recover', runId, signal, clientRequestId)
}

export async function submitRunMessage(
  runId: string, content: string, clientMessageId: string, expectedRevision: number, signal?: AbortSignal,
): Promise<{ client_message_id: string; revision: number; status: string }> {
  return fetchJson(`/v1/runs/${encodeURIComponent(runId)}/messages`, {
    method: 'POST', signal, headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ content, client_message_id: clientMessageId, expected_revision: expectedRevision }),
  })
}

export async function cancelRun(runId: string, signal?: AbortSignal): Promise<BackendRunSnapshot> {
  return fetchJson<BackendRunSnapshot>(`/v1/runs/${encodeURIComponent(runId)}/cancel`, { method: 'POST', signal })
}

export async function getSessionMessages(sessionId = getSessionId()): Promise<unknown> {
  return fetchJson(`/v1/sessions/${encodeURIComponent(sessionId)}/messages`)
}
