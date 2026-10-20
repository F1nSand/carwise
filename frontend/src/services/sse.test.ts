import { describe, expect, it, vi } from 'vitest'
import { parseSseEvents, streamRunEvents } from './sse'

describe('SSE parser', () => {
  it('parses ids, event names, single/multi-line data and ignores keepalive', () => {
    const events = parseSseEvents(
      ': keepalive\n\n' +
        'id: 4\n' +
        'event: tool_call\n' +
        'data: {"step":1,\n' +
        'data: "tool":"recommend"}\n\n',
    )

    expect(events).toEqual([
      {
        id: '4',
        event: 'tool_call',
        data: '{"step":1,\n"tool":"recommend"}',
      },
    ])
  })

  it('does not emit an incomplete empty event', () => {
    expect(parseSseEvents('id: 1\nevent: message\n\n\n')).toEqual([
      { id: '1', event: 'message', data: '' },
    ])
  })

  it('keeps structured runtime events and advances past keepalives', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(
        ': keepalive\n\n' +
          'id: 7\n' +
          'event: input_required\n' +
          'data: {"event_type":"input_required","pending_input":{"questions":[]}}\n\n',
        { status: 200, headers: { 'Content-Type': 'text/event-stream' } },
      ),
    )
    const received: Array<Record<string, unknown>> = []

    await expect(streamRunEvents('/events', 6, (event) => received.push(event))).resolves.toBe(7)
    expect(fetchMock).toHaveBeenCalledWith('/events?after_seq=6', expect.anything())
    expect(fetchMock.mock.calls[0]?.[1]?.credentials).toBe('include')
    expect(received[0]).toMatchObject({ seq: 7, event_type: 'input_required' })
  })

  it('preserves CRLF delimiters split across network chunks', async () => {
    const chunks = [
      'id: 7\r', '\nevent: input_required\r',
      '\ndata: {"event_type":"input_required","pending_input":{"questions":[]}}\r', '\n\r\n',
    ].map((chunk) => new TextEncoder().encode(chunk))
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(new ReadableStream({
      start(controller) { chunks.forEach((chunk) => controller.enqueue(chunk)); controller.close() },
    }), { status: 200, headers: { 'Content-Type': 'text/event-stream' } }))
    const received: Array<Record<string, unknown>> = []

    await expect(streamRunEvents('/events', 6, (event) => received.push(event))).resolves.toBe(7)
    expect(received).toHaveLength(1)
    expect(received[0]).toMatchObject({ seq: 7, event_type: 'input_required' })
  })

  it('keeps a trailing CR pending across an empty network chunk before LF', async () => {
    const chunks = ['id: 1\r', '', '\nevent: message\r', '', '\ndata: {"event_type":"message"}\r\n\r\nid: 2\nevent: message\ndata: {"event_type":"message"}\n\n'].map((chunk) => new TextEncoder().encode(chunk))
    const stream = new ReadableStream<Uint8Array>({
      start(controller) { chunks.forEach((chunk) => controller.enqueue(chunk)); controller.close() },
    })
    vi.stubGlobal('fetch', vi.fn(async () => new Response(stream)))
    const events: BackendRunEvent[] = []
    await streamRunEvents('/events', 0, (event) => events.push(event))
    expect(events.map((event) => event.seq)).toEqual([1, 2])
  })

  it('cancels an open reader when the caller aborts', async () => {
    let cancelled = false
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(
        new ReadableStream({
          start(controller) {
            controller.enqueue(new TextEncoder().encode(': keepalive\n\n'))
          },
          cancel() {
            cancelled = true
          },
        }),
        { status: 200, headers: { 'Content-Type': 'text/event-stream' } },
      ),
    )
    const controller = new AbortController()
    const pending = streamRunEvents('/events', 0, () => undefined, controller.signal)
    setTimeout(() => controller.abort(), 10)

    await expect(
      Promise.race([
        pending,
        new Promise<never>((_, reject) => setTimeout(() => reject(new Error('SSE cancellation timed out')), 100)),
      ]),
    ).rejects.toThrow('SSE 请求已取消')
    expect(fetchMock).toHaveBeenCalled()
    expect(cancelled).toBe(true)
  })
})
