import type { AnalysisResult } from './dashboard'

export type AnalysisIntent = 'recommend' | 'price_compare' | 'evidence' | 'no_data' | 'unknown'

export type AnalysisPhase = 'idle' | 'reviewing' | 'running' | 'awaiting_input' | 'complete' | 'error'

export type RunStatus = 'queued' | 'running' | 'waiting_user' | 'done' | 'error' | 'cancelled'

export type RunOutcome =
  | { kind: 'completed'; result: AnalysisReport }
  | { kind: 'waiting'; pendingInput: PendingInput; checkpoint?: Record<string, unknown> }
  | { kind: 'failed'; failureCode: string; message: string; retryable: boolean; requestId?: string; stage?: string }
  | { kind: 'cancelled'; message: string }

export interface RunSnapshotV2 {
  protocol_version: 2
  run_id: string
  status: RunStatus
  attempt: number
  state_revision: number
  last_event_seq: number
  pending_input?: PendingInput | null
  result?: Record<string, unknown> | null
  error?: string | null
  failure_code?: string | null
  retryable?: boolean
  resume_revision?: number
  plan_revision?: number
}

export type ProgressPhase = 'understanding' | 'querying' | 'calculating' | 'reporting'

export type ProgressStatus = 'queued' | 'running' | 'complete'

export type Confidence = 'high' | 'medium' | 'low' | 'none'

export interface AnalysisFilters {
  budgetMinWan?: number
  budgetMaxWan?: number
  series?: string
  year?: number
  city?: string
  energyType?: string
  bodyType?: string
  maxAge?: number
  maxMileageWanKm?: number
}

export interface Clarification {
  field: 'budget_max_wan'
  question: string
  options: string[]
}

export interface AnalysisDraft {
  id: string
  question: string
  intent: AnalysisIntent
  filters: AnalysisFilters
  title: string
  scenarioLabel: string
  needsClarification: boolean
  clarification?: Clarification
}

export interface AnalysisProgressEvent {
  phase: ProgressPhase
  status: ProgressStatus
  label: string
  detail: string
  percent: number
}

export interface AgentPlanStep {
  id: string
  title: string
  description: string
  toolNames: string[]
  goalIds?: string[]
  /** Backend completion contract (introduced with the durable plan schema). */
  kind?: 'preparation' | 'clarification' | 'data' | 'finalize' | string
  completionCriteria?: string
  status: 'queued' | 'running' | 'complete' | 'skipped'
  reason?: string
}

export interface AgentToolActivity {
  id: string
  toolName: string
  planStepId?: string
  status: 'running' | 'waiting' | 'complete' | 'error'
  resolution?: 'active' | 'handled'
  detail?: string
  output?: Record<string, unknown>
  step?: number
}

export interface AgentQuestionOption {
  id: string
  label: string
  value: string
}

export interface AgentQuestion {
  id: string
  prompt: string
  options: AgentQuestionOption[]
  allow_custom: boolean
  required: boolean
  selection_mode: 'single' | 'multiple'
  max_selections: number
}

export interface PendingInput {
  questions: AgentQuestion[]
  tool_call_id: string
  step: number
  planStepId?: string
  resumeRevision?: number
}

export interface AgentAnswer {
  question_id: string
  values: string[]
  /** @deprecated Only used when reading old local fixtures/checkpoints. */
  value?: string
}

export interface AgentWaitState {
  status: 'waiting_user'
  toolCallId: string
  planStepId?: string
}

export interface AgentTaskGoal {
  id: string
  description: string
  deliverable: string
  dependsOn: string[]
}

export interface AgentRuntimeState {
  plan: AgentPlanStep[]
  activities: AgentToolActivity[]
  pendingInput: PendingInput | null
  waitState: AgentWaitState | null
  status: string
  planRevision?: number
  planChangeReason?: string
  capabilities?: { midrunInput: boolean; recover: boolean }
  inputRevision?: number
  taskMode?: 'understanding' | 'simple' | 'planned'
  taskGoals?: AgentTaskGoal[]
  taskIntentRevision?: number
  taskRevision?: number
  currentStage?: string
}

export type CorrectionStatus = 'idle' | 'submitting' | 'received' | 'applied' | 'error'

export interface PlanUpdateRuntimeEvent {
  kind: 'plan_update'
  seq: number
  plan: AgentPlanStep[]
  revision?: number
  reason?: string
  runId?: string
  attempt?: number
  stateRevision?: number
  planRevision?: number
}

export interface TaskIntentRuntimeEvent {
  kind: 'task_intent_update'
  seq: number
  taskMode: 'simple' | 'planned'
  taskGoals: AgentTaskGoal[]
  revision: number
  runId?: string
  attempt?: number
  stateRevision?: number
  taskRevision?: number
}

export interface ToolRuntimeEvent {
  kind: 'tool_started' | 'tool_completed'
  seq: number
  activity: AgentToolActivity
  runId?: string
  attempt?: number
  stateRevision?: number
}

export interface InputRequiredRuntimeEvent {
  kind: 'input_required'
  seq: number
  pendingInput: PendingInput
  runId?: string
  attempt?: number
  stateRevision?: number
}

export interface LegacyRuntimeEvent extends AnalysisProgressEvent {
  kind: 'legacy_progress'
}

export interface UserMessageRuntimeEvent {
  kind: 'message_received' | 'message_applied'
  seq: number
  clientMessageId: string
  revision?: number
}

export type AnalysisRuntimeEvent = TaskIntentRuntimeEvent | PlanUpdateRuntimeEvent | ToolRuntimeEvent | InputRequiredRuntimeEvent | LegacyRuntimeEvent | UserMessageRuntimeEvent

export interface Reliability {
  confidence: Confidence
  label: string
  sampleSize: number | null
  reason: string
}

export interface ChartDatum {
  label: string
  value: number
  secondaryValue?: number
}

export interface PriceStats {
  p25Wan: number
  medianWan: number
  p75Wan: number
  meanWan: number
  minWan: number
  maxWan: number
  iqrRatio: number
}

export interface Recommendation {
  series: string
  brand: string
  sampleSize: number
  medianWan: number
  p25Wan: number
  p75Wan: number
  medianYear: number
  medianMileageWanKm: number
  score: number
  reason: string
  risk: string
}

export interface RepresentativeListing {
  carName: string
  year: number
  city: string
  priceWan: number
  mileageWanKm: number
}

export interface BaseReport {
  id: string
  question: string
  intent: AnalysisIntent
  scenarioLabel: string
  generatedAt: string
  dataAsOf: string
  summaryTitle: string
  summaryText: string
  reliability: Reliability
  filtersApplied: string[]
  methodology: string
  limitations: string[]
  /** Durable public projection of the run plan, when supplied by the backend. */
  finalPlan?: AgentPlanStep[]
  taskGoals?: AgentTaskGoal[]
  runId?: string
  attempt?: number
  /** Durable conversation/report identity supplied by the report artifact API. */
  conversationId?: string
  reportVersion?: number
  parentReportId?: string | null
  /** Shared, structured analyses attached by the backend when an analysis tool ran. */
  analysisBlocks?: AnalysisResult[]
}

export interface RecommendationReport extends BaseReport {
  intent: 'recommend'
  filters: AnalysisFilters
  budgetCount: number
  candidateCount: number
  recommendations: Recommendation[]
  priceChart: ChartDatum[]
  sampleChart: ChartDatum[]
}

export interface PriceCompareReport extends BaseReport {
  intent: 'price_compare'
  filters: AnalysisFilters
  series: string
  price: PriceStats
  yearChart: ChartDatum[]
  cityChart: ChartDatum[]
  representativeListings: RepresentativeListing[]
  mileageInsight: string
}

export interface AgentAnswerReport extends BaseReport {
  intent: 'unknown'
  answer: string
}

export interface EvidenceQuery {
  filtersApplied: string[]
  matchCount: number
  rows: Record<string, unknown>[]
  columns: string[]
  note: string
}

export interface EvidenceReport extends BaseReport {
  intent: 'evidence'
  answer: string
  evidence: {
    queryCount: number
    queries: EvidenceQuery[]
  }
}

export interface RelaxationCandidate {
  field: string
  action: string
  auto?: boolean
  label?: string
}

export interface NoDataReport extends BaseReport {
  intent: 'no_data'
  answer: string
  reasonCode: string
  observed: Record<string, unknown>
  relaxationCandidates: RelaxationCandidate[]
}

export type AnalysisReport = RecommendationReport | PriceCompareReport | EvidenceReport | NoDataReport | AgentAnswerReport

export interface AnalysisRecord {
  id: string
  question: string
  intent: AnalysisIntent
  scenarioLabel: string
  createdAt: string
  summary: string
  confidence: Confidence
  reportId: string
  status?: 'queued' | 'running' | 'waiting_user' | 'done' | 'error' | 'cancelled'
  retryable?: boolean
  failureCode?: string
  queuePosition?: number
  recoverable?: boolean
  recoveryReason?: string
}

export class AnalysisAdapterError extends Error {
  code: 'empty_input' | 'not_found' | 'invalid_draft' | 'network_error' | 'backend_error' | 'missing_report' | 'stream_error'
  failureCode?: string
  retryable: boolean
  requestId?: string
  stage?: string
  httpStatus?: number

  constructor(message: string, code: AnalysisAdapterError['code'], failureCode?: string, retryable = false, diagnostics: { requestId?: string; stage?: string; httpStatus?: number } = {}) {
    super(message)
    this.name = 'AnalysisAdapterError'
    this.code = code
    this.failureCode = failureCode
    this.retryable = retryable
    Object.assign(this, diagnostics)
  }
}
