import type { PendingInput, RunStatus } from './analysis'

export type ConversationEntryKind =
  | 'user_message'
  | 'assistant_message'
  | 'execution_group'
  | 'question'
  | 'question_answer'
  | 'report_card'
  | 'run_status'

export interface ConversationReportRef {
  reportId: string
  conversationId: string
  originRunId: string
  version: number
  parentReportId?: string | null
  title?: string
  summary?: string
  generatedAt?: string
}

export interface ConversationSummary {
  conversationId: string
  title: string
  titleSource: 'user' | 'generated' | 'first_message' | 'untitled' | string
  createdAt: string
  updatedAt: string
  latestEntryPreview?: string | null
  activeRunId?: string | null
  activeRunStatus?: RunStatus | null
  hasPendingInput: boolean
  latestReport?: ConversationReportRef | null
}

export interface ConversationMetadata extends ConversationSummary {
  contextId: string
  conversationRevision: number
  selectedReportId?: string | null
}

export interface ConversationEntry<TPayload extends Record<string, unknown> = Record<string, unknown>> {
  entryId: string
  conversationId: string
  conversationSeq: number
  kind: ConversationEntryKind
  runId?: string | null
  turnId?: string | null
  sourceKey: string
  payload: TPayload
  createdAt: string
}

export interface ConversationPage {
  conversationId: string
  entries: ConversationEntry[]
  highWaterSeq: number
  nextBeforeSeq?: number | null
}

export interface AcceptedConversationMessage {
  conversationId: string
  acceptedKind: 'run' | 'steering'
  runId: string
  clientRequestId: string
  status: 'queued' | 'accepted'
  inputRevision?: number | null
  entry: ConversationEntry
}

export interface AcceptedConversationMessageWire {
  conversation_id: string
  accepted_kind: 'run' | 'steering'
  run_id: string
  client_request_id: string
  status: 'queued' | 'accepted'
  input_revision?: number | null
  entry: {
    entry_id: string
    conversation_id: string
    conversation_seq: number
    kind: ConversationEntryKind
    run_id?: string | null
    turn_id?: string | null
    source_key: string
    payload?: Record<string, unknown>
    created_at: string
  }
}

export interface PendingQuestionDraft {
  identity: string
  questionIndex: number
  answers: Record<string, string[]>
  customValues: Record<string, string>
}

export interface PendingMessageCommand {
  content: string
  commandId: string
}

export interface ConversationViewState {
  activeConversationId: string | null
  generation: number
  questionSheetOpen: boolean
  reportRouteId: string | null
}

export interface ConversationRunState {
  runId: string
  conversationId: string
  status: RunStatus
  attempt: number
  stateRevision: number
  planRevision: number
  lastReceivedEventSeq: number
  lastAppliedEventSeq: number
  pendingInput: PendingInput | null
}

export interface ConversationState {
  metadata: ConversationMetadata
  entries: ConversationEntry[]
  hasMoreEntries: boolean
  nextBeforeSeq: number | null
  timelineHighWaterSeq: number
  runIds: string[]
  activeRunId: string | null
  selectedReportId: string | null
  composerDraft: string
  scrollAnchor: string | null
  expandedItemIds: string[]
  pendingQuestionDraft: PendingQuestionDraft | null
  pendingMessageCommand: PendingMessageCommand | null
}

export interface ConversationStoreState {
  conversations: Record<string, ConversationState>
  runs: Record<string, ConversationRunState>
  view: ConversationViewState
}

export interface ConversationPageWire {
  conversation_id: string
  entries: Array<{
    entry_id: string
    conversation_id: string
    conversation_seq: number
    kind: ConversationEntryKind
    run_id?: string | null
    turn_id?: string | null
    source_key: string
    payload?: Record<string, unknown>
    created_at: string
  }>
  high_water_seq: number
  next_before_seq?: number | null
}

export interface ConversationSummaryWire {
  conversation_id: string
  title?: string | null
  title_source?: string | null
  created_at: string
  updated_at: string
  latest_entry_preview?: string | { kind?: string; payload?: Record<string, unknown> } | null
  active_run_id?: string | null
  active_run_status?: RunStatus | null
  has_pending_input?: boolean
  latest_report?: {
    report_id: string
    conversation_id: string
    origin_run_id: string
    version: number
    parent_report_id?: string | null
    title?: string
    summary?: string
    generated_at?: string
    created_at?: string
  } | null
}

export interface ConversationMetadataWire extends ConversationSummaryWire {
  context_id: string
  conversation_revision: number
  selected_report_id?: string | null
}
