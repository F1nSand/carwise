import { describe, expect, it } from 'vitest'
import { createEmptyConversationStore, reduceConversationState } from './conversationReducer'
import type { ConversationEntry, ConversationMetadata, ConversationRunState } from '../types/conversation'

function metadata(conversationId: string, revision = 1): ConversationMetadata {
  return {
    conversationId,
    contextId: conversationId,
    title: conversationId,
    titleSource: 'first_message',
    createdAt: '2026-10-06T00:00:00Z',
    updatedAt: '2026-10-06T00:00:00Z',
    conversationRevision: revision,
    hasPendingInput: false,
  }
}

function entry(conversationId: string, entryId: string, seq: number, sourceKey = entryId): ConversationEntry {
  return {
    entryId, conversationId, conversationSeq: seq, kind: 'user_message', sourceKey,
    payload: { text: entryId }, createdAt: '2026-10-06T00:00:00Z',
  }
}

describe('conversation reducer', () => {
  it('merges snapshots and replayed entries by stable identity and sequence', () => {
    let state = reduceConversationState(createEmptyConversationStore(), {
      type: 'conversation_loaded', metadata: metadata('a'),
      page: { conversationId: 'a', entries: [entry('a', 'e2', 2)], highWaterSeq: 2, nextBeforeSeq: 1 },
    })
    state = reduceConversationState(state, { type: 'entry_received', entry: entry('a', 'e1', 1) })
    state = reduceConversationState(state, { type: 'entry_received', entry: entry('a', 'e2', 2) })
    expect(state.conversations.a.entries.map((item) => item.entryId)).toEqual(['e1', 'e2'])
    expect(state.conversations.a.timelineHighWaterSeq).toBe(2)
  })

  it('does not accept another conversation page or late run event into the active view', () => {
    let state = reduceConversationState(createEmptyConversationStore(), { type: 'conversation_loaded', metadata: metadata('a') })
    state = reduceConversationState(state, { type: 'conversation_loaded', metadata: metadata('b') })
    state = reduceConversationState(state, { type: 'set_active_conversation', conversationId: 'b' })
    const original = state
    state = reduceConversationState(state, {
      type: 'entries_loaded', conversationId: 'b', older: true,
      page: { conversationId: 'a', entries: [entry('a', 'secret', 1)], highWaterSeq: 1 },
    })
    expect(state).toBe(original)
    expect(state.conversations.b.entries).toEqual([])
  })

  it('fences stale run revisions and duplicate or prior-attempt events', () => {
    let state = reduceConversationState(createEmptyConversationStore(), { type: 'conversation_loaded', metadata: metadata('a') })
    const run: ConversationRunState = {
      runId: 'r1', conversationId: 'a', status: 'running', attempt: 2, stateRevision: 4,
      planRevision: 1, lastReceivedEventSeq: 6, lastAppliedEventSeq: 6, pendingInput: null,
    }
    state = reduceConversationState(state, { type: 'run_changed', run })
    const same = state
    state = reduceConversationState(state, { type: 'run_changed', run: { ...run, attempt: 1, stateRevision: 99 } })
    expect(state).toBe(same)
    state = reduceConversationState(state, { type: 'run_event_received', runId: 'r1', attempt: 1, seq: 99 })
    state = reduceConversationState(state, { type: 'run_event_received', runId: 'r1', attempt: 2, seq: 6 })
    expect(state.runs.r1.lastReceivedEventSeq).toBe(6)
  })

  it('keeps per-conversation drafts and closes a question sheet on conversation switch', () => {
    let state = reduceConversationState(createEmptyConversationStore(), { type: 'conversation_loaded', metadata: metadata('a') })
    state = reduceConversationState(state, { type: 'conversation_loaded', metadata: metadata('b') })
    state = reduceConversationState(state, { type: 'set_composer_draft', conversationId: 'a', value: 'draft A' })
    state = reduceConversationState(state, { type: 'set_question_sheet', open: true })
    state = reduceConversationState(state, { type: 'set_active_conversation', conversationId: 'b' })
    expect(state.conversations.a.composerDraft).toBe('draft A')
    expect(state.view.questionSheetOpen).toBe(false)
    expect(state.view.generation).toBe(1)
  })

  it('keeps scroll position and expanded timeline items isolated by conversation', () => {
    let state = reduceConversationState(createEmptyConversationStore(), { type: 'conversation_loaded', metadata: metadata('a') })
    state = reduceConversationState(state, { type: 'conversation_loaded', metadata: metadata('b') })
    state = reduceConversationState(state, { type: 'set_scroll_anchor', conversationId: 'a', value: '840' })
    state = reduceConversationState(state, { type: 'set_expanded_item', conversationId: 'a', itemId: 'run:r1:execution', expanded: true })
    state = reduceConversationState(state, { type: 'set_expanded_item', conversationId: 'a', itemId: 'plan:r1:closed', expanded: true })
    expect(state.conversations.a.scrollAnchor).toBe('840')
    expect(state.conversations.a.expandedItemIds).toEqual(['run:r1:execution', 'plan:r1:closed'])
    expect(state.conversations.b.scrollAnchor).toBeNull()
    expect(state.conversations.b.expandedItemIds).toEqual([])
  })
})
