import { afterEach, describe, expect, it, vi } from 'vitest'
import { analyzeQuestion, confirmAnalysis, getReport, listHistory, resumeAnalysis, runtimeFromSnapshot, createRun, retryRun, recoverRun, getRun } from './analysisAdapter'
import type { AnalysisProgressEvent, AgentAnswerReport } from '../types/analysis'

function streamResponse(text: string): Response {
  return new Response(
    new ReadableStream({
      start(controller) {
        controller.enqueue(new TextEncoder().encode(text))
        controller.close()
      },
    }),
    { status: 200, headers: { 'Content-Type': 'text/event-stream' } },
  )
}

function openStreamResponse(closeAfterMs = 250): { response: Response; wasCancelled: () => boolean } {
  let cancelled = false
  let closed = false
  let closeStream = () => undefined
  const response = new Response(
    new ReadableStream({
      start(controller) {
        controller.enqueue(new TextEncoder().encode(': keepalive\n\n'))
        closeStream = () => {
          if (closed) return
          closed = true
          controller.close()
        }
        setTimeout(closeStream, closeAfterMs)
      },
      cancel() {
        cancelled = true
        closed = true
      },
    }),
    { status: 200, headers: { 'Content-Type': 'text/event-stream' } },
  )
  return { response, wasCancelled: () => cancelled }
}

describe('analysis adapter', () => {
  afterEach(() => vi.restoreAllMocks())

  it.each(['queued', 'running', 'done', 'error'] as const)('does not restore a checkpoint question when the run is %s', (status) => {
    const pending = { tool_call_id: 'ask-answered', questions: [{ id: 'budget', prompt: '预算', options: [] }] }
    expect(runtimeFromSnapshot({ run_id: 'resumed', status, input: '选车', resume_revision: 1,
      pending_input: pending, runtime: { pending_input: pending, state: { pending_input: pending } },
    }).pendingInput).toBeNull()
  })

  it('uses the authoritative run revision when an old pending question contains a different counter', () => {
    const runtime = runtimeFromSnapshot({
      run_id: 'legacy-wait', status: 'waiting_user', input: '帮我选车', resume_revision: 0,
      pending_input: { tool_call_id: 'ask', resume_revision: 1,
        questions: [{ id: 'budget', prompt: '预算', options: [], allow_custom: true }] },
    })
    expect(runtime.pendingInput?.resumeRevision).toBe(0)
  })

  it('keeps an underspecified question intact for Agent clarification', async () => {
    const draft = await analyzeQuestion('帮我推荐几款 SUV')
    expect(draft.needsClarification).toBe(false)
    expect(draft.question).toBe('帮我推荐几款 SUV')
    expect(draft.filters).toEqual({})
  })

  it('surfaces the configured input limit returned by the run API', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(Response.json(
      { detail: { code: 'input_too_long', max_characters: 1_500_000 } },
      { status: 413 },
    ))

    const iterator = confirmAnalysis('超长问题', 'session-test', 'context-test')

    await expect(iterator.next()).rejects.toMatchObject({
      message: '输入内容超过当前上限（1,500,000 个字符），请拆成几次分析。',
      failureCode: 'input_too_long',
    })
  })

  it('routes a general question to the Agent instead of inventing a budget clarification', async () => {
    const draft = await analyzeQuestion('你能做什么')
    expect(draft.intent).toBe('unknown')
    expect(draft.needsClarification).toBe(false)
  })

  it('does not infer a vehicle series in the browser', async () => {
    const draft = await analyzeQuestion('宝马 3 系多少钱')
    expect(draft.filters.series).toBeUndefined()
  })

  it('rehydrates the latest safe tool activity and pending question from a run snapshot', () => {
    const runtime = runtimeFromSnapshot({
      run_id: 'run-snapshot',
      session_id: 'session-test',
      input: '预算 12 万',
      status: 'waiting_user',
      plan_revision: 3,
      runtime: {
        plan: [{ id: 'query', title: '查询车源', description: '按预算筛选', tool_names: ['recommend'], status: 'running' }],
        activities: [
          { seq: 4, event_type: 'tool_started', input: { tool_call_id: 'call-1', tool_name: 'recommend', plan_step_id: 'query' }, status: 'running' },
          { seq: 5, event_type: 'tool_completed', input: { tool_call_id: 'call-1', tool_name: 'recommend', plan_step_id: 'query' }, output: { note: '已返回样本' }, status: 'done' },
        ],
      },
      pending_input: {
        tool_call_id: 'call-ask',
        plan_step_id: 'ask',
        step: 2,
        resume_revision: 9,
        questions: [{ id: 'budget', prompt: '预算是多少？', options: [], allow_custom: true, required: true }],
      },
    })

    expect(runtime).toMatchObject({
      plan: [{ id: 'query', toolNames: ['recommend'], status: 'running' }],
      activities: [{ id: 'call-1', status: 'complete', detail: '已返回样本', resolution: 'handled' }],
      pendingInput: { tool_call_id: 'call-ask', planStepId: 'ask', resumeRevision: 9 },
      planRevision: 3,
    })
  })

  it('rehydrates declared task goals and planned mode from a run snapshot', () => {
    const runtime = runtimeFromSnapshot({
      run_id: 'run-task-intent',
      session_id: 'session-test',
      input: '比较车型再推荐',
      status: 'running',
      runtime: {
        task_mode: 'planned',
        task_intent: {
          mode: 'planned',
          needs_plan: true,
          revision: 2,
          goals: [{ id: 'compare', description: '比较车系价格', deliverable: '价格对比', depends_on: [] }],
        },
        plan: [{ id: 'query', title: '查询价格', description: '按目标取数', tool_names: ['price_compare'], goal_ids: ['compare'], status: 'running' }],
      },
    })

    expect(runtime).toMatchObject({
      taskMode: 'planned',
      taskIntentRevision: 2,
      taskGoals: [{ id: 'compare', deliverable: '价格对比' }],
      plan: [{ id: 'query', goalIds: ['compare'] }],
    })
  })

  it('starts a real run, streams backend events and loads the projected report', async () => {
    const report = {
      id: 'run-real',
      question: '15 万以内的 SUV 推荐几款',
      intent: 'recommend',
      scenarioLabel: '智能购车',
      generatedAt: '2026-09-17T00:00:00Z',
      dataAsOf: '项目清洗车源数据',
      summaryTitle: '预算内车系推荐',
      summaryText: '完成',
      reliability: { confidence: 'high', label: '较高可靠性', sampleSize: 10, reason: '样本充分' },
      filtersApplied: [],
      methodology: '统计口径',
      limitations: [],
      filters: { budgetMaxWan: 15 },
      budgetCount: 100,
      candidateCount: 3,
      recommendations: [],
      priceChart: [],
      sampleChart: [],
    }
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      expect(init?.credentials).toBe('include')
      const url = String(input)
      if (url.endsWith('/v1/reports/run-real')) {
        return Response.json({ detail: { code: 'report_not_found' } }, { status: 404 })
      }
      if (url.endsWith('/v1/runs')) {
        const request = JSON.parse(String(init?.body))
        expect(request).toMatchObject({
          input: '15 万以内的 SUV 推荐几款',
          session_id: 'session-test',
          context_id: 'context-test',
          filters: {
            budget_min_wan: null,
            budget_max_wan: null,
            series: null,
            year: null,
            city: null,
            energy_type: null,
            body_type: null,
            max_age: null,
            max_mileage_wan_km: null,
          },
        })
        expect(request.client_request_id).toEqual(expect.any(String))
        return Response.json({ run_id: 'run-real', session_id: 'session-test', status: 'running' }, { status: 202 })
      }
      if (url.includes('/events')) {
        return streamResponse(
          'id: 1\nevent: started\ndata: {"event_type":"started"}\n\n' +
            'id: 2\nevent: tool_call\ndata: {"event_type":"tool_call"}\n\n' +
            'id: 3\nevent: context\ndata: {"event_type":"context"}\n\n' +
            'id: 4\nevent: final\ndata: {"event_type":"final"}\n\n' +
            'id: 5\nevent: done\ndata: {"event_type":"done"}\n\n',
        )
      }
      if (url.endsWith('/v1/runs/run-real')) {
        return Response.json({ run_id: 'run-real', status: 'done', result: { report } }, { status: 200 })
      }
      throw new Error(`unexpected request: ${url}`)
    })

    const draft = await analyzeQuestion('15 万以内的 SUV 推荐几款')
    const events: AnalysisProgressEvent[] = []
    for await (const event of confirmAnalysis(draft, 'session-test', 'context-test')) events.push(event)
    const finalReport = await getReport('run-real')

    expect(fetchMock).toHaveBeenCalled()
    expect(events.map((event) => event.phase)).toEqual([
      'understanding',
      'querying',
      'calculating',
      'reporting',
    ])
    expect(finalReport).toMatchObject({ ...report, runId: 'run-real', reportVersion: 1, parentReportId: null })
  })

  it('returns the report from a terminal SSE event without waiting for snapshot', async () => {
    const report = { id: 'run-fast', intent: 'recommend', summaryText: 'PRIVATE REPORT' } as AnalysisReport
    const debugSpy = vi.spyOn(console, 'debug').mockImplementation(() => undefined)
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.endsWith('/v1/runs')) {
        return Response.json({ run_id: 'run-fast', session_id: 'session-test', status: 'running' }, { status: 202 })
      }
      if (url.includes('/events')) {
        return streamResponse(
          'id: 1\nevent: final\ndata: {"event_type":"final"}\n\n'
            + `id: 2\nevent: done\ndata: ${JSON.stringify({ event_type: 'done', result: { report } })}\n\n`,
        )
      }
      throw new Error(`snapshot should not be requested after terminal report: ${url}`)
    })

    const iterator = confirmAnalysis('直接回答一个问题', 'session-test', 'context-test')
    const progress = await iterator.next()
    expect(progress.done).toBe(false)
    expect(progress.value?.kind).toBe('legacy_progress')

    const completed = await iterator.next()

    expect(fetchMock).toHaveBeenCalledTimes(2)
    expect(completed.done).toBe(true)
    expect(completed.value).toEqual(report)
    const debugText = debugSpy.mock.calls.flat().map(String).join(' ')
    expect(debugText).toContain('terminal_event')
    expect(debugText).not.toContain('PRIVATE REPORT')
  })

  it('preserves the backend failure code from the terminal SSE event', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.endsWith('/v1/reports/run-real')) return Response.json({ detail: { code: 'report_not_found' } }, { status: 404 })
      if (url.endsWith('/v1/runs')) {
        return Response.json({ run_id: 'run-timeout', session_id: 'session-test', status: 'running' }, { status: 202 })
      }
      if (url.includes('/events')) {
        return streamResponse(
          'id: 1\nevent: error\ndata: {"seq":1,"event_type":"error","error":"模型响应超时。","failure_code":"model_timeout","retryable":true}\n\n',
        )
      }
      throw new Error(`unexpected request: ${url}`)
    })

    const iterator = confirmAnalysis('查询宝马 3 系', 'session-test', 'context-test')

    await expect(iterator.next()).rejects.toMatchObject({
      message: '模型响应超时。',
      failureCode: 'model_timeout',
    })
  })

  it('loads history through the backend adapter', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      Response.json(
        [
          {
            id: 'run-history',
            question: '问题',
            intent: 'recommend',
            scenarioLabel: '智能购车',
            createdAt: '2026-09-17T00:00:00Z',
            summary: '结论',
            confidence: 'high',
            reportId: 'run-history',
            status: 'done',
          },
        ],
        { status: 200 },
      ),
    )

    await expect(listHistory('session-test')).resolves.toHaveLength(1)
  })

  it('returns a direct Agent answer instead of turning it into a missing report error', async () => {
    const answer: AgentAnswerReport = {
      id: 'run-answer',
      question: '二手车价格应该怎么看',
      intent: 'unknown',
      scenarioLabel: 'Agent 问答',
      generatedAt: '2026-09-17T00:00:00Z',
      dataAsOf: '项目清洗车源数据',
      summaryTitle: 'Agent 回答',
      summaryText: '先看挂牌价和中位价。',
      answer: '先看挂牌价和中位价。',
      reliability: { confidence: 'none', label: '暂无结论', sampleSize: 0, reason: '本次未调用结构化数据工具。' },
      filtersApplied: [],
      methodology: '本次由 Agent 直接回答，未调用结构化数据工具。',
      limitations: ['本次未调用结构化数据工具，回答不包含价格统计。'],
    }
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.endsWith('/v1/reports/run-answer')) return Response.json({ detail: { code: 'report_not_found' } }, { status: 404 })
      return Response.json({ run_id: 'run-answer', context_id: 'conversation-answer', status: 'done', result: { report: answer } }, { status: 200 })
    })

    await expect(getReport('run-answer')).resolves.toMatchObject({
      ...answer, conversationId: 'conversation-answer', reportVersion: 1, runId: 'run-answer',
    })
  })

  it('loads immutable report metadata and content from the conversation report endpoint', async () => {
    const content = { id: 'run-v2', intent: 'unknown', question: '改为上海', summaryText: '已更新' } as AnalysisReport
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(Response.json({
      report_id: 'run-v2', conversation_id: 'conversation-1', origin_run_id: 'run-v2', version: 2,
      parent_report_id: 'run-v1', content, created_at: '2026-10-06T01:00:00Z',
    }))

    await expect(getReport('run-v2')).resolves.toMatchObject({
      id: 'run-v2', conversationId: 'conversation-1', reportVersion: 2,
      parentReportId: 'run-v1', runId: 'run-v2',
    })
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(String(fetchMock.mock.calls[0][0])).toContain('/v1/reports/run-v2')
  })

  it('reconnects an interrupted SSE stream from the last sequence', async () => {
    let eventRequests = 0
    let snapshotRequests = 0
    const debugSpy = vi.spyOn(console, 'debug').mockImplementation(() => undefined)
    const report = { id: 'run-reconnect', intent: 'recommend' } as const
    const transportStates: string[] = []
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.endsWith('/v1/runs')) {
        return Response.json({ run_id: 'run-reconnect', session_id: 'session-test', status: 'running' }, { status: 202 })
      }
      if (url.includes('/events')) {
        eventRequests += 1
        if (eventRequests === 1) {
          expect(url).toContain('after_seq=0')
          return streamResponse('id: 1\nevent: started\ndata: {"event_type":"started"}\n\n')
        }
        expect(url).toContain('after_seq=1')
        return streamResponse('id: 2\nevent: done\ndata: {"event_type":"done"}\n\n')
      }
      if (url.endsWith('/v1/runs/run-reconnect')) {
        snapshotRequests += 1
        if (snapshotRequests === 1) return Response.json({ run_id: 'run-reconnect', status: 'running' }, { status: 200 })
        return Response.json({ run_id: 'run-reconnect', status: 'done', result: { report } }, { status: 200 })
      }
      throw new Error(`unexpected request: ${url}`)
    })

    const draft = await analyzeQuestion('15 万以内的 SUV 推荐几款')
    const events: AnalysisProgressEvent[] = []
    for await (const event of confirmAnalysis(draft, 'session-test', 'context-test', {
      onTransportState: (state) => transportStates.push(state),
    })) events.push(event)

    expect(fetchMock).toHaveBeenCalled()
    expect(eventRequests).toBe(2)
    expect(snapshotRequests).toBe(2)
    expect(events.map((event) => event.phase)).toEqual(['understanding', 'reporting'])
    expect(transportStates).toEqual(['live', 'reconnecting', 'live', 'offline'])
    expect(debugSpy.mock.calls.flat().map(String).join(' ')).toContain('stream_reconnect')
  })

  it('returns a completed snapshot while the SSE connection remains open', async () => {
    let snapshotRequests = 0
    let streamState: ReturnType<typeof openStreamResponse> | undefined
    const report = { id: 'run-watchdog', intent: 'recommend', summaryText: 'PRIVATE WATCHDOG REPORT' } as const
    const debugSpy = vi.spyOn(console, 'debug').mockImplementation(() => undefined)
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.endsWith('/v1/runs')) {
        return Response.json({ run_id: 'run-watchdog', session_id: 'session-test', status: 'running' }, { status: 202 })
      }
      if (url.includes('/events')) {
        streamState = openStreamResponse()
        return streamState.response
      }
      if (url.endsWith('/v1/runs/run-watchdog')) {
        snapshotRequests += 1
        return Response.json({ run_id: 'run-watchdog', status: 'done', result: { report } }, { status: 200 })
      }
      throw new Error(`unexpected request: ${url}`)
    })

    const iterator = confirmAnalysis('半开连接也要完成', 'session-test', 'context-test', { statusPollIntervalMs: 10 })
    const completed = await Promise.race([
      iterator.next(),
      new Promise<never>((_, reject) => setTimeout(() => reject(new Error('snapshot watchdog timed out')), 100)),
    ])

    expect(completed.done).toBe(true)
    expect(completed.value).toEqual(report)
    expect(snapshotRequests).toBe(1)
    expect(streamState?.wasCancelled()).toBe(true)
    const debugText = debugSpy.mock.calls.flat().map(String).join(' ')
    expect(debugText).toContain('snapshot_watchdog')
    expect(debugText).not.toContain('PRIVATE WATCHDOG REPORT')
  })

  it('returns waiting_user from a snapshot while the SSE connection remains open', async () => {
    let snapshotRequests = 0
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.endsWith('/v1/runs')) {
        return Response.json({ run_id: 'run-waiting', session_id: 'session-test', status: 'running' }, { status: 202 })
      }
      if (url.includes('/events')) return openStreamResponse().response
      if (url.endsWith('/v1/runs/run-waiting')) {
        snapshotRequests += 1
        return Response.json({
          run_id: 'run-waiting',
          status: 'waiting_user',
          pending_input: {
            tool_call_id: 'call-watchdog',
            step: 1,
            questions: [{ id: 'budget', prompt: '预算是多少？', options: [], allow_custom: true, required: true }],
          },
        }, { status: 200 })
      }
      throw new Error(`unexpected request: ${url}`)
    })

    const iterator = confirmAnalysis('需要补充条件', 'session-test', 'context-test', { statusPollIntervalMs: 10 })
    const waiting = await Promise.race([
      iterator.next(),
      new Promise<never>((_, reject) => setTimeout(() => reject(new Error('waiting snapshot watchdog timed out')), 100)),
    ])

    expect(waiting.done).toBe(false)
    expect(waiting.value).toMatchObject({
      kind: 'input_required',
      pendingInput: { tool_call_id: 'call-watchdog', questions: [expect.objectContaining({ id: 'budget' })] },
    })
    await expect(iterator.next()).resolves.toMatchObject({ done: true, value: null })
    expect(snapshotRequests).toBe(1)
  })

  it('does not treat the old waiting state as the terminal state of a resumed run', async () => {
    let eventRequests = 0
    let snapshotRequests = 0
    const report = { id: 'run-resume-transition', intent: 'recommend' } as const
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.includes('/events')) {
        eventRequests += 1
        if (eventRequests === 1) return streamResponse(': keepalive\n\n')
        return streamResponse(
          `id: 17\nevent: done\ndata: ${JSON.stringify({ event_type: 'done', result: { report } })}\n\n`,
        )
      }
      if (url.endsWith('/v1/runs/run-resume-transition')) {
        snapshotRequests += 1
        if (snapshotRequests === 1) return Response.json({ run_id: 'run-resume-transition', status: 'waiting_user' }, { status: 200 })
        return Response.json({ run_id: 'run-resume-transition', status: 'done', result: { report } }, { status: 200 })
      }
      throw new Error(`unexpected request: ${url}`)
    })

    const iterator = resumeAnalysis('run-resume-transition', { afterSeq: 16, statusPollIntervalMs: 10 })
    const completed = await Promise.race([
      (async () => {
        let next = await iterator.next()
        while (!next.done) next = await iterator.next()
        return next
      })(),
      new Promise<never>((_, reject) => setTimeout(() => reject(new Error('resume transition timed out')), 200)),
    ])

    expect(completed.done).toBe(true)
    expect(completed.value).toEqual(report)
    expect(eventRequests).toBe(2)
    expect(snapshotRequests).toBe(1)
  })

  it('returns immediately when resume receives a newer input_required tool call', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.includes('/events')) {
        return streamResponse(
          'id: 18\nevent: input_required\ndata: ' + JSON.stringify({
            seq: 18,
            event_type: 'input_required',
            input: { tool_call_id: 'call-b' },
            pending_input: {
              tool_call_id: 'call-b',
              step: 2,
              questions: [{ id: 'brand', prompt: '品牌？', options: [], allow_custom: true, required: true }],
            },
          }) + '\n\n',
        )
      }
      throw new Error(`unexpected request: ${url}`)
    })

    const iterator = resumeAnalysis('run-new-question', {
      afterSeq: 17,
      answeredToolCallId: 'call-a',
      statusPollIntervalMs: 10,
    })
    const first = await Promise.race([
      iterator.next(),
      new Promise<never>((_, reject) => setTimeout(() => reject(new Error('new question timed out')), 100)),
    ])
    expect(first.done).toBe(false)
    expect(first.value?.kind).toBe('input_required')
    const completed = await Promise.race([
      iterator.next(),
      new Promise<never>((_, reject) => setTimeout(() => reject(new Error('resume did not stop')), 100)),
    ])
    expect(completed.done).toBe(true)
    expect(completed.value).toBeNull()
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it.each(['call-a', undefined])('returns a new question from the snapshot without waiting for the resume transition timeout (answered: %s)', async (answeredToolCallId) => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      if (String(input).includes('/events')) return openStreamResponse().response
      return Response.json({ run_id: 'run-next', status: 'waiting_user', attempt: 2, resume_revision: 1,
        pending_input: { tool_call_id: 'call-b', questions: [{ id: 'brand', prompt: '品牌？', options: [] }] } })
    })
    const iterator = resumeAnalysis('run-next', { answeredToolCallId, statusPollIntervalMs: 10 })
    expect(await iterator.next()).toMatchObject({ done: false, value: { kind: 'input_required', pendingInput: { tool_call_id: 'call-b' } } })
    expect(await iterator.next()).toEqual({ done: true, value: null })
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })

  it('skips replay of the accepted tool call even after its revision and attempt change', async () => {
    const report = { id: 'run-accepted', intent: 'recommend' }
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(streamResponse(
      'id: 18\nevent: input_required\ndata: ' + JSON.stringify({ seq: 18, event_type: 'input_required', attempt: 2,
        resume_revision: 1, pending_input: { tool_call_id: 'call-a', questions: [{ id: 'budget', prompt: '预算', options: [] }] } })
      + '\n\nid: 19\nevent: done\ndata: ' + JSON.stringify({ event_type: 'done', result: { report } }) + '\n\n',
    ))
    const iterator = resumeAnalysis('run-accepted', { answeredToolCallId: 'call-a' })
    expect(await iterator.next()).toMatchObject({ done: false, value: { kind: 'legacy_progress' } })
    expect(await iterator.next()).toEqual({ done: true, value: report })
  })

  it('stops the open SSE reader when the watchdog observes a backend error', async () => {
    let streamState: ReturnType<typeof openStreamResponse> | undefined
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.endsWith('/v1/runs')) {
        return Response.json({ run_id: 'run-watchdog-error', session_id: 'session-test', status: 'running' }, { status: 202 })
      }
      if (url.includes('/events')) {
        streamState = openStreamResponse()
        return streamState.response
      }
      if (url.endsWith('/v1/runs/run-watchdog-error')) {
        return Response.json({ error: '模型服务暂时不可用' }, { status: 500 })
      }
      throw new Error(`unexpected request: ${url}`)
    })

    const iterator = confirmAnalysis('验证错误收口', 'session-test', 'context-test', { statusPollIntervalMs: 10 })
    await expect(
      Promise.race([
        iterator.next(),
        new Promise<never>((_, reject) => setTimeout(() => reject(new Error('error watchdog timed out')), 100)),
      ]),
    ).rejects.toMatchObject({ code: 'backend_error' })
    expect(streamState?.wasCancelled()).toBe(true)
  })

  it('aborts both stream and watchdog when caller cancels', async () => {
    let snapshotRequests = 0
    let streamState: ReturnType<typeof openStreamResponse> | undefined
    const controller = new AbortController()
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.endsWith('/v1/runs')) {
        return Response.json({ run_id: 'run-cancel', session_id: 'session-test', status: 'running' }, { status: 202 })
      }
      if (url.includes('/events')) {
        streamState = openStreamResponse()
        return streamState.response
      }
      if (url.endsWith('/v1/runs/run-cancel')) {
        snapshotRequests += 1
        return Response.json({ run_id: 'run-cancel', status: 'running' }, { status: 200 })
      }
      throw new Error(`unexpected request: ${url}`)
    })

    const iterator = confirmAnalysis('取消这次分析', 'session-test', 'context-test', {
      signal: controller.signal,
      statusPollIntervalMs: 10,
    })
    const pending = iterator.next()
    setTimeout(() => controller.abort(), 10)
    await expect(
      Promise.race([
        pending,
        new Promise<never>((_, reject) => setTimeout(() => reject(new Error('cancellation timed out')), 100)),
      ]),
    ).rejects.toMatchObject({ code: 'stream_error' })
    expect(snapshotRequests).toBe(0)
    expect(streamState?.wasCancelled()).toBe(true)
  })
})


describe('submission identity and structured errors', () => {
  afterEach(() => vi.restoreAllMocks())
  it('preserves structured diagnostics and does not automatically retry POST', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(Response.json({ detail: {
      code: 'storage_busy', message: '暂时无法受理', retryable: true, request_id: 'trace-1', stage: 'persist',
    } }, { status: 503 }))
    await expect(createRun('问题', 'session', 'context', {}, undefined, 'command-1')).rejects.toMatchObject({
      message: '暂时无法受理', failureCode: 'storage_busy', retryable: true, requestId: 'trace-1', stage: 'persist', httpStatus: 503,
    })
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })
  it('keeps unknown 500 nonretryable while allowing explicit same-key confirmation', async () => {
    const bodies: string[] = []
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (_url, init) => {
      bodies.push(String(init?.body))
      if (bodies.length === 1) return Response.json({ detail: { code: 'internal_error', message: '状态不明', retryable: false } }, { status: 500 })
      return Response.json({ run_id: 'only-run', status: 'queued' }, { status: 202 })
    })
    await expect(createRun('问题', 'session', 'context', {}, undefined, 'stable-create')).rejects.toMatchObject({ retryable: false })
    await createRun('问题', 'session', 'context', {}, undefined, 'stable-create')
    expect(bodies[1]).toBe(bodies[0])
  })
  it.each(['retry', 'recover'] as const)('retains %s identity after response loss and releases it after acceptance', async (kind) => {
    const bodies: string[] = []
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (_url, init) => {
      bodies.push(String(init?.body))
      if (bodies.length === 1) throw new TypeError('response lost')
      return Response.json({ run_id: 'accepted', status: 'queued' })
    })
    const send = kind === 'retry' ? retryRun : recoverRun
    await expect(send(`lost-${kind}`)).rejects.toMatchObject({ code: 'network_error' })
    await send(`lost-${kind}`)
    await send(`lost-${kind}`)
    expect(bodies[1]).toBe(bodies[0])
    expect(bodies[2]).not.toBe(bodies[1])
  })
  it('compensates a temporary snapshot 503 with bounded reads', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(Response.json({ detail: { code: 'storage_busy', message: 'busy', retryable: true } }, { status: 503 }))
      .mockResolvedValueOnce(Response.json({ run_id: 'snapshot-run', status: 'running' }))
    expect(await getRun('snapshot-run')).toMatchObject({ status: 'running' })
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })
})


describe('failure capability parity', () => {
  afterEach(() => vi.restoreAllMocks())
  it.each(['sse', 'snapshot'] as const)('keeps retryability when failure is delivered through %s', async (source) => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (url) => {
      if (String(url).endsWith('/v1/runs')) return Response.json({ run_id: 'failed-capability', status: 'queued' })
      if (String(url).includes('/events')) return streamResponse(source === 'sse'
        ? 'id: 1\nevent: error\ndata: ' + JSON.stringify({ event_type: 'error', error: '暂时失败', failure_code: 'model_timeout', retryable: true, request_id: 'trace-run', stage: 'model' }) + '\n\n'
        : ': keepalive\n\n')
      return Response.json({ run_id: 'failed-capability', status: 'error', error: '暂时失败', failure_code: 'model_timeout', retryable: true, request_id: 'trace-run', stage: 'model' })
    })
    const iterator = confirmAnalysis('问题', 'session', 'context')
    await expect((async () => { while (!(await iterator.next()).done) { /* consume */ } })()).rejects.toMatchObject({
      failureCode: 'model_timeout', retryable: true, requestId: 'trace-run', stage: 'model',
    })
  })
  it('normalizes historical failure capabilities from the public wire response', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(Response.json([{ id: 'history-error', retryable: true, failure_code: 'model_timeout' }]))
    expect(await listHistory('session')).toMatchObject([{ retryable: true, failureCode: 'model_timeout' }])
  })
})
