import type {
  ConversationEntry,
  ConversationMetadata,
  ConversationPage,
  ConversationRunState,
  ConversationState,
  ConversationStoreState,
} from '../types/conversation'

export type ConversationAction =
  | { type: 'conversation_loaded'; metadata: ConversationMetadata; page?: ConversationPage }
  | { type: 'entries_loaded'; conversationId: string; page: ConversationPage; older: boolean }
  | { type: 'entry_received'; entry: ConversationEntry }
  | { type: 'run_changed'; run: ConversationRunState }
  | { type: 'run_event_received'; runId: string; seq: number; attempt: number }
  | { type: 'run_event_applied'; runId: string; seq: number; attempt: number }
  | { type: 'set_active_conversation'; conversationId: string | null }
  | { type: 'set_question_sheet'; open: boolean }
  | { type: 'set_report_route'; reportId: string | null }
  | { type: 'set_composer_draft'; conversationId: string; value: string }
  | { type: 'set_scroll_anchor'; conversationId: string; value: string | null }
  | { type: 'toggle_entry_expanded'; conversationId: string; entryId: string }
  | { type: 'set_expanded_item'; conversationId: string; itemId: string; expanded: boolean }
  | { type: 'set_question_draft'; conversationId: string; value: ConversationState['pendingQuestionDraft'] }
  | { type: 'set_pending_message'; conversationId: string; value: ConversationState['pendingMessageCommand'] }
  | { type: 'set_selected_report'; conversationId: string; reportId: string | null }

export function emptyConversationState(metadata: ConversationMetadata): ConversationState {
  return {
    metadata,
    entries: [],
    hasMoreEntries: false,
    nextBeforeSeq: null,
    timelineHighWaterSeq: 0,
    runIds: [],
    activeRunId: metadata.activeRunId || null,
    selectedReportId: metadata.selectedReportId || metadata.latestReport?.reportId || null,
    composerDraft: '',
    scrollAnchor: null,
    expandedItemIds: [],
    pendingQuestionDraft: null,
    pendingMessageCommand: null,
  }
}

export function mergeEntries(
  existing: ConversationEntry[],
  incoming: ConversationEntry[],
): ConversationEntry[] {
  const merged: ConversationEntry[] = []
  for (const entry of [...existing, ...incoming]) {
    const index = merged.findIndex((item) => item.entryId === entry.entryId || item.sourceKey === entry.sourceKey)
    if (index < 0) merged.push(entry)
    else merged[index] = { ...merged[index], ...entry, payload: { ...merged[index].payload, ...entry.payload } }
  }
  return merged.sort((left, right) =>
    left.conversationSeq - right.conversationSeq || left.entryId.localeCompare(right.entryId),
  )
}

function withConversation(state: ConversationStoreState, conversationId: string): ConversationState | null {
  return state.conversations[conversationId] || null
}

function replaceConversation(state: ConversationStoreState, conversation: ConversationState): ConversationStoreState {
  return { ...state, conversations: { ...state.conversations, [conversation.metadata.conversationId]: conversation } }
}

export function reduceConversationState(
  state: ConversationStoreState,
  action: ConversationAction,
): ConversationStoreState {
  switch (action.type) {
    case 'conversation_loaded': {
      const id = action.metadata.conversationId
      const current = state.conversations[id]
      const base = current || emptyConversationState(action.metadata)
      const page = action.page
      const conversation: ConversationState = {
        ...base,
        metadata: action.metadata.conversationRevision >= base.metadata.conversationRevision
          ? action.metadata
          : base.metadata,
        activeRunId: action.metadata.activeRunId || null,
        selectedReportId: action.metadata.selectedReportId || action.metadata.latestReport?.reportId || null,
        entries: page
          ? mergeEntries(base.entries, page.entries.filter((entry) => entry.conversationId === id))
          : base.entries,
        timelineHighWaterSeq: Math.max(base.timelineHighWaterSeq, page?.highWaterSeq || 0),
        nextBeforeSeq: page ? page.nextBeforeSeq ?? null : base.nextBeforeSeq,
        hasMoreEntries: page ? page.nextBeforeSeq != null : base.hasMoreEntries,
      }
      return replaceConversation(state, conversation)
    }
    case 'entries_loaded': {
      const current = withConversation(state, action.conversationId)
      if (!current || action.page.conversationId !== action.conversationId) return state
      const conversation: ConversationState = {
        ...current,
        entries: mergeEntries(current.entries, action.page.entries.filter((entry) => entry.conversationId === action.conversationId)),
        timelineHighWaterSeq: Math.max(current.timelineHighWaterSeq, action.page.highWaterSeq),
        nextBeforeSeq: action.older ? action.page.nextBeforeSeq ?? null : current.nextBeforeSeq,
        hasMoreEntries: action.older ? action.page.nextBeforeSeq != null : current.hasMoreEntries,
      }
      return replaceConversation(state, conversation)
    }
    case 'entry_received': {
      const current = withConversation(state, action.entry.conversationId)
      if (!current) return state
      return replaceConversation(state, {
        ...current,
        entries: mergeEntries(current.entries, [action.entry]),
        timelineHighWaterSeq: Math.max(current.timelineHighWaterSeq, action.entry.conversationSeq),
        runIds: action.entry.runId && !current.runIds.includes(action.entry.runId)
          ? [...current.runIds, action.entry.runId]
          : current.runIds,
      })
    }
    case 'run_changed': {
      const previous = state.runs[action.run.runId]
      if (previous && (
        action.run.attempt < previous.attempt
        || (action.run.attempt === previous.attempt && action.run.stateRevision < previous.stateRevision)
      )) return state
      const conversation = state.conversations[action.run.conversationId]
      const nextRuns = { ...state.runs, [action.run.runId]: action.run }
      if (!conversation) return { ...state, runs: nextRuns }
      return replaceConversation({ ...state, runs: nextRuns }, {
        ...conversation,
        runIds: conversation.runIds.includes(action.run.runId)
          ? conversation.runIds
          : [...conversation.runIds, action.run.runId],
        activeRunId: ['queued', 'running', 'waiting_user'].includes(action.run.status)
          ? action.run.runId
          : conversation.activeRunId === action.run.runId ? null : conversation.activeRunId,
      })
    }
    case 'run_event_received': {
      const previous = state.runs[action.runId]
      if (!previous || action.attempt !== previous.attempt || action.seq <= previous.lastReceivedEventSeq) return state
      return { ...state, runs: { ...state.runs, [action.runId]: { ...previous, lastReceivedEventSeq: action.seq } } }
    }
    case 'run_event_applied': {
      const previous = state.runs[action.runId]
      if (!previous || action.attempt !== previous.attempt || action.seq <= previous.lastAppliedEventSeq || action.seq > previous.lastReceivedEventSeq) return state
      return { ...state, runs: { ...state.runs, [action.runId]: { ...previous, lastAppliedEventSeq: action.seq } } }
    }
    case 'set_active_conversation':
      return {
        ...state,
        view: {
          ...state.view,
          activeConversationId: action.conversationId,
          generation: state.view.generation + 1,
          questionSheetOpen: false,
          reportRouteId: null,
        },
      }
    case 'set_question_sheet':
      return { ...state, view: { ...state.view, questionSheetOpen: action.open } }
    case 'set_report_route':
      return { ...state, view: { ...state.view, reportRouteId: action.reportId } }
    case 'set_composer_draft': {
      const current = withConversation(state, action.conversationId)
      return current ? replaceConversation(state, { ...current, composerDraft: action.value }) : state
    }
    case 'set_scroll_anchor': {
      const current = withConversation(state, action.conversationId)
      return current ? replaceConversation(state, { ...current, scrollAnchor: action.value }) : state
    }
    case 'toggle_entry_expanded': {
      const current = withConversation(state, action.conversationId)
      if (!current) return state
      const expanded = new Set(current.expandedItemIds)
      if (expanded.has(action.entryId)) expanded.delete(action.entryId)
      else expanded.add(action.entryId)
      return replaceConversation(state, { ...current, expandedItemIds: [...expanded] })
    }
    case 'set_expanded_item': {
      const current = withConversation(state, action.conversationId)
      if (!current) return state
      const expanded = new Set(current.expandedItemIds)
      if (action.expanded) expanded.add(action.itemId)
      else expanded.delete(action.itemId)
      return replaceConversation(state, { ...current, expandedItemIds: [...expanded] })
    }
    case 'set_question_draft': {
      const current = withConversation(state, action.conversationId)
      return current ? replaceConversation(state, { ...current, pendingQuestionDraft: action.value }) : state
    }
    case 'set_pending_message': {
      const current = withConversation(state, action.conversationId)
      return current ? replaceConversation(state, { ...current, pendingMessageCommand: action.value }) : state
    }
    case 'set_selected_report': {
      const current = withConversation(state, action.conversationId)
      return current ? replaceConversation(state, { ...current, selectedReportId: action.reportId }) : state
    }
    default:
      return state
  }
}

export function createEmptyConversationStore(): ConversationStoreState {
  return {
    conversations: {},
    runs: {},
    view: { activeConversationId: null, generation: 0, questionSheetOpen: false, reportRouteId: null },
  }
}
