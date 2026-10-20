export interface RawSseEvent {
  id?: string
  event?: string
  data: string
}

export interface BackendRunEvent extends Record<string, unknown> {
  seq: number
  event_type: string
}

export interface SseParser {
  feed(chunk: string): void
  end(): void
}

function createParser(onEvent: (event: RawSseEvent) => void): SseParser {
  let buffer = ''
  let pendingCarriageReturn = false
  let id: string | undefined
  let event: string | undefined
  let data: string[] = []

  function dispatch() {
    if (id !== undefined || event !== undefined || data.length > 0) {
      onEvent({ id, event, data: data.join('\n') })
    }
    id = undefined
    event = undefined
    data = []
  }

  function line(value: string) {
    if (value === '') {
      dispatch()
      return
    }
    if (value.startsWith(':')) return
    const separator = value.indexOf(':')
    const field = separator === -1 ? value : value.slice(0, separator)
    const rawValue = separator === -1 ? '' : value.slice(separator + 1).replace(/^ /, '')
    if (field === 'id') id = rawValue
    else if (field === 'event') event = rawValue
    else if (field === 'data') data.push(rawValue)
  }

  return {
    feed(chunk: string) {
      if (!chunk) return
      let index = 0
      if (pendingCarriageReturn) {
        pendingCarriageReturn = false
        line(buffer)
        buffer = ''
        if (chunk.startsWith('\n')) index = 1
      }
      for (; index < chunk.length; index += 1) {
        const character = chunk[index]
        if (character === '\r') {
          if (index === chunk.length - 1) {
            pendingCarriageReturn = true
          } else {
            line(buffer)
            buffer = ''
            if (chunk[index + 1] === '\n') index += 1
          }
        } else if (character === '\n') {
          line(buffer)
          buffer = ''
        } else {
          buffer += character
        }
      }
    },
    end() {
      if (pendingCarriageReturn) {
        line(buffer)
        buffer = ''
        pendingCarriageReturn = false
      }
      if (buffer.length > 0) line(buffer)
      dispatch()
      buffer = ''
    },
  }
}

export function parseSseEvents(text: string): RawSseEvent[] {
  const events: RawSseEvent[] = []
  const parser = createParser((event) => events.push(event))
  parser.feed(text)
  parser.end()
  return events
}

export async function streamRunEvents(
  url: string,
  afterSeq: number,
  onEvent: (event: BackendRunEvent) => void,
  signal?: AbortSignal,
  onConnected?: () => void,
): Promise<number> {
  const separator = url.includes('?') ? '&' : '?'
  const response = await fetch(`${url}${separator}after_seq=${Math.max(0, afterSeq)}`, {
    headers: { Accept: 'text/event-stream' },
    signal,
    credentials: 'include',
  })
  if (!response.ok) throw new Error(`SSE 请求失败（HTTP ${response.status}）`)
  if (!response.body) throw new Error('SSE 响应没有可读取的数据流')
  onConnected?.()

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let lastSeq = Math.max(0, afterSeq)
  let aborted = false
  const abortReader = () => {
    aborted = true
    void reader.cancel().catch(() => undefined)
  }
  signal?.addEventListener('abort', abortReader, { once: true })
  if (signal?.aborted) abortReader()
  const parser = createParser((raw) => {
    if (!raw.data || raw.data.trim() === '') return
    let payload: Record<string, unknown>
    try {
      payload = JSON.parse(raw.data) as Record<string, unknown>
    } catch (reason) {
      throw new Error(`SSE 数据格式非法：${reason instanceof Error ? reason.message : String(reason)}`)
    }
    const seq = Number(raw.id ?? payload.seq ?? 0)
    if (!Number.isFinite(seq) || seq <= lastSeq) return
    lastSeq = seq
    onEvent({ ...payload, seq, event_type: String(payload.event_type ?? raw.event ?? 'message') })
  })

  try {
    while (true) {
      const chunk = await reader.read()
      if (chunk.done) break
      parser.feed(decoder.decode(chunk.value, { stream: true }))
    }
    parser.feed(decoder.decode())
    parser.end()
  } finally {
    signal?.removeEventListener('abort', abortReader)
    reader.releaseLock()
  }
  if (aborted || signal?.aborted) throw new Error('SSE 请求已取消')
  return lastSeq
}
