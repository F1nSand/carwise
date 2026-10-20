import { describe, expect, it, vi } from 'vitest'
import type { ConversationAdapter } from '../services/conversationAdapter'
import type { ConversationMetadata, ConversationPage } from '../types/conversation'
import { createConversationStore } from './conversationStore'

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => { resolve = done })
  return { promise, resolve }
}

function metadata(id: string): ConversationMetadata {
  return {
    conversationId: id, contextId: id, title: id, titleSource: 'first_message',
    createdAt: '2026-10-06T00:00:00Z', updatedAt: '2026-10-06T00:00:00Z',
    conversationRevision: 1, hasPendingInput: false,
  }
}

function page(id: string): ConversationPage {
  return {
    conversationId: id, highWaterSeq: 1,
    entries: [{
      entryId: `entry-${id}`, conversationId: id, conversationSeq: 1, kind: 'user_message',
      sourceKey: `message-${id}`, payload: { text: id }, createdAt: '2026-10-06T00:00:00Z',
    }],
  }
}

describe('conversation store', () => {
  it('ignores a stale load when the user has switched conversations', async () => {
    const aMetadata = deferred<ConversationMetadata>()
    const aPage = deferred<ConversationPage>()
    const api: ConversationAdapter = {
      createConversation: vi.fn(),
      listConversations: vi.fn(),
      getConversation: vi.fn((id) => id === 'a' ? aMetadata.promise : Promise.resolve(metadata(id))),
      getConversationEntries: vi.fn((id) => id === 'a' ? aPage.promise : Promise.resolve(page(id))),
      sendMessage: vi.fn(),
    }
    const store = createConversationStore(api)
    const openingA = store.openConversation('a')
    await store.openConversation('b')
    aMetadata.resolve(metadata('a'))
    aPage.resolve(page('a'))
    await openingA

    expect(store.state.view.activeConversationId).toBe('b')
    expect(store.getConversation('b')?.entries.map((entry) => entry.entryId)).toEqual(['entry-b'])
    expect(store.getConversation('a')).toBeUndefined()
  })

  it('creates a fresh conversation and preserves each draft independently', async () => {
    const api: ConversationAdapter = {
      createConversation: vi.fn(async () => ({ conversationId: 'new', contextId: 'new', metadata: metadata('new') })),
      listConversations: vi.fn(),
      getConversation: vi.fn(),
      getConversationEntries: vi.fn(),
      sendMessage: vi.fn(),
    }
    const store = createConversationStore(api)
    const id = await store.createConversation()
    store.setDraft(id, 'follow-up')
    expect(id).toBe('new')
    expect(store.state.view.activeConversationId).toBe('new')
    expect(store.getConversation(id)?.composerDraft).toBe('follow-up')
  })
})
