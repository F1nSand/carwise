import { describe, expect, it } from 'vitest'
import { conversationAdapter, conversationMetadataFromWire, conversationPageFromWire, conversationSummaryFromWire } from './conversationAdapter'

describe('conversation wire adapter', () => {
  it('normalizes summary metadata and immutable report references', () => {
    const summary = conversationSummaryFromWire({
      conversation_id: 'c1', title: '上海选车', title_source: 'first_message',
      created_at: '2026-10-06T00:00:00Z', updated_at: '2026-10-06T01:00:00Z',
      latest_entry_preview: '推荐几款车', active_run_id: null, active_run_status: null,
      has_pending_input: false,
      latest_report: {
        report_id: 'report-v2', conversation_id: 'c1', origin_run_id: 'r2', version: 2,
        parent_report_id: 'report-v1', title: '预算内车型推荐', summary: '匹配到 5 款车', generated_at: '2026-10-06T01:00:00Z',
      },
    })
    expect(summary.conversationId).toBe('c1')
    expect(summary.latestReport).toMatchObject({ reportId: 'report-v2', version: 2, parentReportId: 'report-v1' })

    const metadata = conversationMetadataFromWire({
      conversation_id: 'c1', context_id: 'c1', title: '上海选车', title_source: 'first_message',
      created_at: '2026-10-06T00:00:00Z', updated_at: '2026-10-06T01:00:00Z',
      conversation_revision: 4, selected_report_id: 'report-v1',
    })
    expect(metadata.contextId).toBe('c1')
    expect(metadata.conversationRevision).toBe(4)
    expect(metadata.selectedReportId).toBe('report-v1')
  })

  it('keeps stable entry sequence and high-water pagination cursor', () => {
    const page = conversationPageFromWire({
      conversation_id: 'c1', high_water_seq: 17, next_before_seq: 9,
      entries: [{
        entry_id: 'e17', conversation_id: 'c1', conversation_seq: 17, kind: 'assistant_message',
        run_id: 'r1', turn_id: 't1', source_key: 'answer:r1', payload: { content: '完成' }, created_at: '2026-10-06T01:00:00Z',
      }],
    })
    expect(page).toMatchObject({ conversationId: 'c1', highWaterSeq: 17, nextBeforeSeq: 9 })
    expect(page.entries[0]).toMatchObject({ entryId: 'e17', runId: 'r1', conversationSeq: 17, payload: { content: '完成' } })
  })

  it('posts a single idempotent command and maps the accepted timeline entry', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(Response.json({
      conversation_id: 'c1', accepted_kind: 'steering', run_id: 'r1', client_request_id: 'cmd-1',
      status: 'accepted', input_revision: 3,
      entry: {
        entry_id: 'e9', conversation_id: 'c1', conversation_seq: 9, kind: 'user_message',
        run_id: 'r1', source_key: 'message:cmd-1', payload: { text: '改成上海', delivery_status: 'accepted' },
        created_at: '2026-10-06T01:00:00Z',
      },
    }))

    const accepted = await conversationAdapter.sendMessage('c1', '改成上海', 'cmd-1', 'report-v2')

    expect(accepted).toMatchObject({ acceptedKind: 'steering', runId: 'r1', status: 'accepted', inputRevision: 3 })
    expect(accepted.entry).toMatchObject({ entryId: 'e9', conversationSeq: 9, sourceKey: 'message:cmd-1' })
    expect(JSON.parse(String(fetchMock.mock.calls[0][1]?.body))).toEqual({
      content: '改成上海', client_request_id: 'cmd-1', source_report_id: 'report-v2',
    })
  })
})
