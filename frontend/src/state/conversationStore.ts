import { reactive } from 'vue'
import { conversationAdapter, type ConversationAdapter } from '../services/conversationAdapter'
import type { ConversationState, ConversationStoreState } from '../types/conversation'
import {
  createEmptyConversationStore,
  reduceConversationState,
} from './conversationReducer'

export class ConversationStore {
  readonly state: ConversationStoreState

  constructor(private readonly api: ConversationAdapter = conversationAdapter) {
    this.state = reactive(createEmptyConversationStore()) as ConversationStoreState
  }

  dispatch(action: Parameters<typeof reduceConversationState>[1]): void {
    Object.assign(this.state, reduceConversationState(this.state, action))
  }

  getConversation(conversationId: string): ConversationState | undefined {
    return this.state.conversations[conversationId]
  }

  async createConversation(activate = true): Promise<string> {
    const created = await this.api.createConversation()
    this.dispatch({ type: 'conversation_loaded', metadata: created.metadata })
    if (activate) this.dispatch({ type: 'set_active_conversation', conversationId: created.conversationId })
    return created.conversationId
  }

  async openConversation(conversationId: string, signal?: AbortSignal): Promise<void> {
    this.dispatch({ type: 'set_active_conversation', conversationId })
    const generation = this.state.view.generation
    const [metadata, page] = await Promise.all([
      this.api.getConversation(conversationId, signal),
      this.api.getConversationEntries(conversationId, null, 50, signal),
    ])
    if (this.state.view.activeConversationId !== conversationId || this.state.view.generation !== generation) return
    this.dispatch({ type: 'conversation_loaded', metadata, page })
  }

  async loadOlderEntries(conversationId: string, signal?: AbortSignal): Promise<void> {
    const current = this.getConversation(conversationId)
    if (!current?.hasMoreEntries || current.nextBeforeSeq == null) return
    const generation = this.state.view.generation
    const page = await this.api.getConversationEntries(conversationId, current.nextBeforeSeq, 50, signal)
    if (this.state.view.activeConversationId !== conversationId || this.state.view.generation !== generation) return
    this.dispatch({ type: 'entries_loaded', conversationId, page, older: true })
  }

  setDraft(conversationId: string, value: string): void {
    this.dispatch({ type: 'set_composer_draft', conversationId, value })
  }
}

export function createConversationStore(api?: ConversationAdapter): ConversationStore {
  return new ConversationStore(api)
}
