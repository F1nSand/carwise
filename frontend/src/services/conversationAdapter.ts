import { AnalysisAdapterError } from '../types/analysis'
import type {
  ConversationMetadata,
  ConversationMetadataWire,
  AcceptedConversationMessage,
  AcceptedConversationMessageWire,
  ConversationPage,
  ConversationPageWire,
  ConversationReportRef,
  ConversationSummary,
  ConversationSummaryWire,
} from '../types/conversation'

const apiBaseUrl = String(import.meta.env.VITE_API_BASE_URL || '').replace(/\/$/, '')

export interface ConversationListPage {
  items: ConversationSummary[]
  nextCursor: string | null
}

export interface CreateConversationResult {
  conversationId: string
  contextId: string
  metadata: ConversationMetadata
}

export interface ConversationAdapter {
  createConversation(commandId?: string): Promise<CreateConversationResult>
  listConversations(cursor?: string | null, limit?: number, signal?: AbortSignal): Promise<ConversationListPage>
  getConversation(conversationId: string, signal?: AbortSignal): Promise<ConversationMetadata>
  getConversationEntries(conversationId: string, beforeSeq?: number | null, limit?: number, signal?: AbortSignal): Promise<ConversationPage>
  sendMessage(conversationId: string, content: string, commandId: string, sourceReportId?: string | null, signal?: AbortSignal): Promise<AcceptedConversationMessage>
}

function apiUrl(path: string): string {
  return `${apiBaseUrl}${path}`
}

function newCommandId(): string {
  return globalThis.crypto?.randomUUID?.() ?? `cmd-${Date.now()}-${Math.random().toString(36).slice(2)}`
}

async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response
  try {
    response = await fetch(apiUrl(path), { ...init, credentials: 'include' })
  } catch (error) {
    throw new AnalysisAdapterError(
      `无法连接会话服务：${error instanceof Error ? error.message : String(error)}`,
      'network_error', undefined, true,
    )
  }
  if (!response.ok) {
    let detail = `HTTP ${response.status}`
    try {
      const body = await response.json() as { detail?: string | { message?: string; code?: string } }
      const value = body.detail
      detail = typeof value === 'string' ? value : value?.message || value?.code || detail
    } catch { /* keep HTTP status */ }
    throw new AnalysisAdapterError(detail, response.status === 404 ? 'not_found' : 'backend_error', undefined, response.status >= 500, { httpStatus: response.status })
  }
  try {
    return await response.json() as T
  } catch {
    throw new AnalysisAdapterError('会话服务返回了无法解析的数据。', 'backend_error')
  }
}

function reportFromWire(report: ConversationSummaryWire['latest_report']): ConversationReportRef | null {
  if (!report) return null
  return {
    reportId: report.report_id,
    conversationId: report.conversation_id,
    originRunId: report.origin_run_id,
    version: report.version,
    parentReportId: report.parent_report_id,
    title: report.title,
    summary: report.summary,
    generatedAt: report.generated_at || report.created_at,
  }
}

export function conversationSummaryFromWire(wire: ConversationSummaryWire): ConversationSummary {
  const preview = typeof wire.latest_entry_preview === 'string'
    ? wire.latest_entry_preview
    : wire.latest_entry_preview?.payload
      ? String(wire.latest_entry_preview.payload.text || wire.latest_entry_preview.payload.content || wire.latest_entry_preview.payload.summary || '')
      : null
  return {
    conversationId: wire.conversation_id,
    title: wire.title?.trim() || '新会话',
    titleSource: wire.title_source || 'untitled',
    createdAt: wire.created_at,
    updatedAt: wire.updated_at,
    latestEntryPreview: preview,
    activeRunId: wire.active_run_id,
    activeRunStatus: wire.active_run_status,
    hasPendingInput: wire.has_pending_input === true,
    latestReport: reportFromWire(wire.latest_report),
  }
}

export function conversationMetadataFromWire(wire: ConversationMetadataWire): ConversationMetadata {
  return {
    ...conversationSummaryFromWire(wire),
    contextId: wire.context_id,
    conversationRevision: wire.conversation_revision,
    selectedReportId: wire.selected_report_id,
  }
}

export function conversationPageFromWire(wire: ConversationPageWire): ConversationPage {
  return {
    conversationId: wire.conversation_id,
    entries: wire.entries.map((entry) => ({
      entryId: entry.entry_id,
      conversationId: entry.conversation_id,
      conversationSeq: entry.conversation_seq,
      kind: entry.kind,
      runId: entry.run_id,
      turnId: entry.turn_id,
      sourceKey: entry.source_key,
      payload: entry.payload || {},
      createdAt: entry.created_at,
    })),
    highWaterSeq: wire.high_water_seq,
    nextBeforeSeq: wire.next_before_seq,
  }
}

export function conversationEntryFromWire(entry: AcceptedConversationMessageWire['entry']): ConversationPage['entries'][number] {
  return {
    entryId: entry.entry_id,
    conversationId: entry.conversation_id,
    conversationSeq: entry.conversation_seq,
    kind: entry.kind,
    runId: entry.run_id,
    turnId: entry.turn_id,
    sourceKey: entry.source_key,
    payload: entry.payload || {},
    createdAt: entry.created_at,
  }
}

export const conversationAdapter: ConversationAdapter = {
  async createConversation(commandId = newCommandId()) {
    const wire = await requestJson<ConversationMetadataWire & { client_request_id?: string }>('/v1/conversations', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ client_request_id: commandId }),
    })
    const metadata = conversationMetadataFromWire(wire)
    return { conversationId: metadata.conversationId, contextId: metadata.contextId, metadata }
  },

  async listConversations(cursor, limit = 30, signal) {
    const params = new URLSearchParams({ limit: String(limit) })
    if (cursor) params.set('cursor', cursor)
    const wire = await requestJson<{ items: ConversationSummaryWire[]; next_cursor?: string | null }>(`/v1/conversations?${params.toString()}`, { signal })
    return { items: wire.items.map(conversationSummaryFromWire), nextCursor: wire.next_cursor || null }
  },

  async getConversation(conversationId, signal) {
    const wire = await requestJson<ConversationMetadataWire>(`/v1/conversations/${encodeURIComponent(conversationId)}`, { signal })
    return conversationMetadataFromWire(wire)
  },

  async getConversationEntries(conversationId, beforeSeq, limit = 50, signal) {
    const params = new URLSearchParams({ limit: String(limit) })
    if (beforeSeq != null) params.set('before_seq', String(beforeSeq))
    const wire = await requestJson<ConversationPageWire>(`/v1/conversations/${encodeURIComponent(conversationId)}/entries?${params.toString()}`, { signal })
    return conversationPageFromWire(wire)
  },

  async sendMessage(conversationId, content, commandId, sourceReportId, signal) {
    const wire = await requestJson<AcceptedConversationMessageWire>(`/v1/conversations/${encodeURIComponent(conversationId)}/messages`, {
      method: 'POST', signal,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        content,
        client_request_id: commandId,
        ...(sourceReportId ? { source_report_id: sourceReportId } : {}),
      }),
    })
    return {
      conversationId: wire.conversation_id,
      acceptedKind: wire.accepted_kind,
      runId: wire.run_id,
      clientRequestId: wire.client_request_id,
      status: wire.status,
      inputRevision: wire.input_revision,
      entry: conversationEntryFromWire(wire.entry),
    }
  },
}
