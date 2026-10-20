import { afterEach, describe, expect, it, vi } from 'vitest'
import { useAnalysisFlow } from '../composables/useAnalysisFlow'

function closedEventStream(): Response {
  return new Response(
    new ReadableStream({
      start(controller) {
        controller.enqueue(new TextEncoder().encode(': heartbeat\n\n'))
        controller.close()
      },
    }),
    { status: 200, headers: { 'Content-Type': 'text/event-stream' } },
  )
}

describe('run lifecycle across the real adapter and page flow', () => {
  afterEach(() => vi.restoreAllMocks())

  it('hydrates a waiting question from SQLite snapshot when the SSE notification is missing', async () => {
    const pendingInput = {
      tool_call_id: 'call-snapshot-question',
      step: 2,
      questions: [{
        id: 'budget',
        prompt: '你的预算是多少？',
        options: [{ id: 'a', label: '15 万以内', value: '15' }],
        allow_custom: true,
        required: true,
      }],
    }
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.endsWith('/v1/runs')) {
        return Response.json({ run_id: 'run-waiting-snapshot', session_id: 'session-test', status: 'queued' }, { status: 202 })
      }
      if (url.includes('/events')) return closedEventStream()
      if (url.endsWith('/v1/runs/run-waiting-snapshot')) {
        return Response.json({
          run_id: 'run-waiting-snapshot',
          session_id: 'session-test',
          input: '帮我选车',
          status: 'waiting_user',
          pending_input: pendingInput,
        }, { status: 200 })
      }
      throw new Error(`unexpected request: ${url}`)
    })

    const flow = useAnalysisFlow()
    await flow.submit('帮我选车')

    expect(flow.phase.value).toBe('awaiting_input')
    expect(flow.runtime.value.pendingInput).toMatchObject({
      tool_call_id: 'call-snapshot-question',
      questions: [expect.objectContaining({ id: 'budget', prompt: '你的预算是多少？' })],
    })
    expect(flow.error.value).toBe('')
  })

  it('accepts a committed done snapshot even when the last plan event was lost', async () => {
    const report = { id: 'run-done-snapshot', intent: 'recommend', summaryText: '有结果' }
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.endsWith('/v1/runs')) {
        return Response.json({ run_id: 'run-done-snapshot', session_id: 'session-test', status: 'queued' }, { status: 202 })
      }
      if (url.includes('/events')) return closedEventStream()
      if (url.endsWith('/v1/runs/run-done-snapshot')) {
        return Response.json({
          run_id: 'run-done-snapshot',
          session_id: 'session-test',
          input: '帮我选车',
          status: 'done',
          runtime: { plan: [{ id: 'final', title: '整理答案', description: '', tool_names: [], status: 'running' }] },
          result: { report },
        }, { status: 200 })
      }
      throw new Error(`unexpected request: ${url}`)
    })

    const flow = useAnalysisFlow()
    await flow.submit('帮我选车')

    expect(flow.phase.value).toBe('complete')
    expect(flow.runtime.value.status).toBe('done')
    expect(flow.report.value).toEqual(report)
    expect(flow.error.value).toBe('')
  })
})
