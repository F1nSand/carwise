import { beforeEach, describe, expect, it, vi } from 'vitest'
import { retainAnalysisFlow, useAnalysisFlow } from './useAnalysisFlow'
import { AnalysisAdapterError, type AnalysisDraft, type AnalysisProgressEvent, type AnalysisReport } from '../types/analysis'

const adapter = vi.hoisted(() => ({
  analyzeQuestion: vi.fn(),
  confirmAnalysis: vi.fn(),
  resumeAnalysis: vi.fn(),
  submitRunInput: vi.fn(),
  getSessionId: vi.fn(() => 'session-test'),
  retryRun: vi.fn(),
  getRun: vi.fn(),
  runtimeFromSnapshot: vi.fn(),
  normalizePlan: vi.fn((steps) => Array.isArray(steps) ? steps : []),
  submitRunMessage: vi.fn(),
  createAnalysisContextId: vi.fn(() => 'context-test-1'),
  cancelRun: vi.fn(),
}))

vi.mock('../services/analysisAdapter', () => adapter)

const draft: AnalysisDraft = {
  id: 'draft-1',
  question: '15 万以内的 SUV 推荐几款',
  intent: 'recommend',
  filters: { budgetMaxWan: 15, bodyType: 'SUV' },
  title: '确认你的购车条件',
  scenarioLabel: '智能购车',
  needsClarification: false,
}

const report = { id: 'run-1', intent: 'recommend' } as AnalysisReport

function setupConfirmGenerator() {
  adapter.confirmAnalysis.mockImplementation(async function* (
    _value: string,
    _sessionId: string,
    _contextId: string,
    options: { onRunStarted?: (runId: string) => void; onEvent?: (seq: number) => void } = {},
  ): AsyncGenerator<AnalysisProgressEvent & { kind: 'legacy_progress' }, AnalysisReport> {
    options.onRunStarted?.('run-1')
    yield { kind: 'legacy_progress', phase: 'understanding', status: 'running', label: '理解购车条件', detail: '收到', percent: 25 }
    yield { kind: 'legacy_progress', phase: 'reporting', status: 'complete', label: '整理结论与图表', detail: '完成', percent: 100 }
    return report
  })
}

describe('useAnalysisFlow', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    adapter.getSessionId.mockReturnValue('session-test')
    adapter.runtimeFromSnapshot.mockReturnValue({ plan: [], activities: [], pendingInput: null })
    adapter.analyzeQuestion.mockResolvedValue(draft)
    setupConfirmGenerator()
  })

  it('replaces the live plan with the completed report plan when the final update was missed', async () => {
    const plan = [{ id: 'finalize', title: '整理答案', description: '', toolNames: ['finalize_answer'], status: 'running' }]
    const finalPlan = [{ ...plan[0], status: 'complete' as const }]
    adapter.confirmAnalysis.mockImplementation(async function* () {
      yield { kind: 'plan_update', seq: 1, revision: 1, plan }
      return { ...report, finalPlan }
    })
    const flow = useAnalysisFlow()
    await flow.submit('你好')
    expect(flow.phase.value).toBe('complete')
    expect(flow.runtime.value.plan).toEqual(finalPlan)
  })

  it('uses a stable history session and a fresh analysis context', async () => {
    const flow = useAnalysisFlow()

    await flow.submit(draft.question)

    expect(flow.sessionId.value).toBe('session-test')
    expect(flow.contextId.value).toBe('context-test-1')
    expect(flow.runId.value).toBe('run-1')
    expect(flow.lastEventSeq.value).toBe(0)
    expect(flow.phase.value).toBe('complete')
    expect(flow.reportId.value).toBe('run-1')
    expect(adapter.confirmAnalysis).toHaveBeenCalledWith(
      draft.question,
      'session-test',
      'context-test-1',
      expect.objectContaining({ onRunStarted: expect.any(Function) }),
    )
  })

  it('closes an accepted question immediately and ignores its stale snapshot and replay while the Agent runs', async () => {
    const flow = useAnalysisFlow()
    const pendingInput = { tool_call_id: 'ask-submitted', step: 1, resumeRevision: 0,
      questions: [{ id: 'budget', prompt: '预算', options: [], allow_custom: true, required: true }] }
    flow.runId.value = 'answer-sheet'
    flow.phase.value = 'awaiting_input'
    flow.runtime.value = { ...flow.runtime.value, pendingInput, status: 'waiting_user' }
    adapter.submitRunInput.mockResolvedValue({ accepted_tool_call_id: 'ask-submitted', resume_revision: 1 })
    adapter.runtimeFromSnapshot.mockReturnValue({ plan: [], activities: [], pendingInput })
    let finish!: () => void
    const continuation = new Promise<void>((resolve) => { finish = resolve })
    adapter.resumeAnalysis.mockImplementation(async function* (_id, options) {
      // Worker resumption advances both counters while an old wait checkpoint
      // can still be visible. Neither counter creates a new question.
      adapter.runtimeFromSnapshot.mockReturnValue({ plan: [], activities: [], pendingInput: { ...pendingInput, resumeRevision: 1 } })
      options.onSnapshot?.({ run_id: 'answer-sheet', status: 'running', resume_revision: 1, attempt: 2 })
      yield { kind: 'input_required', seq: 7, attempt: 2, pendingInput: { ...pendingInput, resumeRevision: 1 } }
      await continuation
      return report
    })
    const submission = flow.submitInput([{ question_id: 'budget', values: ['15'] }])
    await vi.waitFor(() => expect(adapter.resumeAnalysis).toHaveBeenCalled())
    expect(flow.isSubmitting.value).toBe(false)
    expect(flow.runtime.value.pendingInput).toBeNull()
    expect(flow.phase.value).toBe('running')
    finish()
    await submission
    expect(flow.phase.value).toBe('complete')
  })

  it('opens a newer question after acceptance without keeping its submit button busy', async () => {
    const flow = useAnalysisFlow()
    const pendingInput = { tool_call_id: 'ask-reused', step: 1, resumeRevision: 0,
      questions: [{ id: 'choice', prompt: '选择', options: [], allow_custom: true, required: true }] }
    flow.runId.value = 'answer-next'
    flow.runtime.value = { ...flow.runtime.value, pendingInput, status: 'waiting_user' }
    adapter.submitRunInput.mockResolvedValue({ accepted_tool_call_id: 'ask-reused', resume_revision: 1 })
    adapter.resumeAnalysis.mockImplementation(async function* () {
      yield { kind: 'input_required', seq: 8, attempt: 2, pendingInput: { ...pendingInput, tool_call_id: 'ask-next', resumeRevision: 1 } }
      return null
    })
    await flow.submitInput([{ question_id: 'choice', values: ['A'] }])
    expect(flow.isSubmitting.value).toBe(false)
    expect(flow.runtime.value.pendingInput?.resumeRevision).toBe(1)
    expect(flow.runtime.value.pendingInput?.tool_call_id).toBe('ask-next')
    expect(flow.phase.value).toBe('awaiting_input')
  })

  it('ends submitting at HTTP acceptance rather than at the end of the continuation stream', async () => {
    const flow = useAnalysisFlow()
    flow.runId.value = 'answer-running'
    flow.runtime.value.pendingInput = { tool_call_id: 'ask-running', step: 1,
      questions: [{ id: 'choice', prompt: '选择', options: [], allow_custom: true, required: true }] }
    adapter.submitRunInput.mockResolvedValue({ accepted_tool_call_id: 'ask-running' })
    let finish!: () => void
    const continuation = new Promise<void>((resolve) => { finish = resolve })
    adapter.resumeAnalysis.mockImplementation(async function* () { await continuation; return report })
    const submission = flow.submitInput([{ question_id: 'choice', values: ['A'] }])
    await vi.waitFor(() => expect(adapter.resumeAnalysis).toHaveBeenCalled())
    expect(flow.isSubmitting.value).toBe(false)
    expect(flow.runtime.value.pendingInput).toBeNull()
    expect(flow.phase.value).toBe('running')
    finish()
    await submission
  })

  it('creates a new context for a second homepage question without changing history session', async () => {
    adapter.createAnalysisContextId.mockReturnValueOnce('context-first').mockReturnValueOnce('context-second')
    const flow = useAnalysisFlow()

    await flow.submit('第一条问题')
    await flow.submit('第二条问题')

    expect(flow.sessionId.value).toBe('session-test')
    expect(flow.contextId.value).toBe('context-second')
    expect(adapter.createAnalysisContextId).toHaveBeenCalledTimes(2)
  })

  it('preserves the confirmed filter payload when starting the run', async () => {
    const flow = useAnalysisFlow()
    await flow.submit(draft.question)
    const confirmedFilters = { budgetMaxWan: 12, bodyType: 'SUV', maxAge: 3 }

    await flow.confirm(confirmedFilters)

    expect(adapter.confirmAnalysis).toHaveBeenLastCalledWith(
      { ...draft, filters: confirmedFilters },
      'session-test',
      'context-test-1',
      expect.any(Object),
    )
  })

  it('ignores a late run acceptance after the flow is reset', async () => {
    let releaseAcceptance!: () => void
    const acceptance = new Promise<void>((resolve) => { releaseAcceptance = resolve })
    adapter.confirmAnalysis.mockImplementation(async function* (
      _value: string,
      _sessionId: string,
      _contextId: string,
      options: { onRunStarted?: (runId: string) => void } = {},
    ) {
      await acceptance
      options.onRunStarted?.('late-run')
      return report
    })
    const flow = useAnalysisFlow()

    const pending = flow.submit(draft.question)
    await vi.waitFor(() => expect(adapter.confirmAnalysis).toHaveBeenCalled())
    flow.reset()
    releaseAcceptance()
    await pending

    expect(flow.phase.value).toBe('idle')
    expect(flow.runId.value).toBe('')
    expect(flow.report.value).toBeNull()
  })

  it('sends underspecified natural language directly to the Agent', async () => {
    const flow = useAnalysisFlow()
    const incomplete = { ...draft, needsClarification: true, filters: {} }
    adapter.analyzeQuestion.mockResolvedValue(incomplete)

    await flow.submit('帮我推荐 SUV')

    expect(flow.phase.value).toBe('complete')
    expect(flow.draft.value?.needsClarification).toBe(true)
    expect(adapter.confirmAnalysis).toHaveBeenCalledWith(
      '帮我推荐 SUV',
      'session-test',
      'context-test-1',
      expect.any(Object),
    )
  })

  it('pauses on Agent questions and resumes with all answers', async () => {
    const flow = useAnalysisFlow()
    adapter.confirmAnalysis.mockImplementation(async function* (
      _question: string,
      _session: string,
      _context: string,
      options: { onRunStarted?: (runId: string) => void } = {},
    ) {
      options.onRunStarted?.('run-wait')
      yield {
        kind: 'input_required',
        seq: 3,
        pendingInput: {
          tool_call_id: 'call-ask',
          step: 1,
          questions: [{ id: 'budget', prompt: '预算？', options: [], allow_custom: true, required: true }],
        },
      }
      return null
    })
    adapter.submitRunInput.mockResolvedValue(undefined)
    adapter.resumeAnalysis.mockImplementation(async function* () {
      yield { kind: 'legacy_progress', phase: 'reporting', status: 'complete', label: '完成', detail: '完成', percent: 100 }
      return report
    })

    await flow.submit('帮我选车')
    expect(flow.phase.value).toBe('awaiting_input')
    expect(flow.runtime.value.pendingInput?.questions[0].id).toBe('budget')

    await flow.submitInput([{ question_id: 'budget', value: '15' }])

    expect(adapter.submitRunInput).toHaveBeenCalledWith(
      'run-wait',
      [{ question_id: 'budget', value: '15' }],
      { toolCallId: 'call-ask' },
      expect.any(AbortSignal),
      expect.any(String),
    )
    expect(flow.phase.value).toBe('complete')
    expect(flow.reportId.value).toBe('run-1')
  })

  it('does not let the superseded initial stream overwrite a completed resume stream', async () => {
    const flow = useAnalysisFlow()
    let releaseInitialStream!: () => void
    adapter.confirmAnalysis.mockImplementation(async function* (
      _question: string,
      _session: string,
      _context: string,
      options: { onRunStarted?: (runId: string) => void; signal?: AbortSignal } = {},
    ) {
      options.onRunStarted?.('run-race')
      yield {
        kind: 'input_required',
        seq: 3,
        pendingInput: {
          tool_call_id: 'call-race',
          step: 1,
          questions: [{ id: 'budget', prompt: '预算？', options: [], allow_custom: true, required: true }],
        },
      }
      await new Promise<never>((_, reject) => {
        releaseInitialStream = () => reject(new Error('分析事件流读取失败：SSE 请求已取消'))
        options.signal?.addEventListener('abort', () => undefined, { once: true })
      })
    })
    adapter.submitRunInput.mockResolvedValue(undefined)
    adapter.resumeAnalysis.mockImplementation(async function* () {
      return report
    })

    const initialRun = flow.submit('15 万以内的 SUV 推荐几款')
    for (let attempt = 0; attempt < 10 && flow.phase.value !== 'awaiting_input'; attempt += 1) {
      await Promise.resolve()
    }
    expect(flow.phase.value).toBe('awaiting_input')

    await flow.submitInput([{ question_id: 'budget', value: '15' }])
    expect(flow.phase.value).toBe('complete')
    expect(flow.reportId.value).toBe('run-1')

    releaseInitialStream()
    await initialRun

    expect(flow.phase.value).toBe('complete')
    expect(flow.reportId.value).toBe('run-1')
    expect(flow.error.value).toBe('')
  })

  it('locks duplicate answer submissions while the Agent is resuming', async () => {
    const flow = useAnalysisFlow()
    let releaseSubmit!: () => void
    adapter.confirmAnalysis.mockImplementation(async function* (
      _question: string,
      _session: string,
      _context: string,
      options: { onRunStarted?: (runId: string) => void } = {},
    ) {
      options.onRunStarted?.('run-wait')
      yield {
        kind: 'input_required',
        seq: 3,
        pendingInput: {
          tool_call_id: 'call-ask',
          step: 1,
          questions: [{ id: 'budget', prompt: '预算？', options: [], allow_custom: true, required: true }],
        },
      }
      return null
    })
    adapter.submitRunInput.mockImplementation(() => new Promise<void>((resolve) => { releaseSubmit = resolve }))
    adapter.resumeAnalysis.mockImplementation(async function* () {
      return report
    })

    await flow.submit('帮我选车')
    const first = flow.submitInput([{ question_id: 'budget', value: '15' }])
    await Promise.resolve()
    const second = flow.submitInput([{ question_id: 'budget', value: '15' }])

    expect(flow.isSubmitting.value).toBe(true)
    expect(adapter.submitRunInput).toHaveBeenCalledTimes(1)
    expect(flow.runtime.value.activities[0]).toMatchObject({
      status: 'running',
      resolution: 'active',
      detail: '正在提交回答，等待 Agent 恢复',
    })
    releaseSubmit()
    await Promise.all([first, second])
    expect(flow.phase.value).toBe('complete')
  })

  it('deduplicates runtime events and closes handled plan/tool states after resume', async () => {
    const flow = useAnalysisFlow()
    adapter.confirmAnalysis.mockImplementation(async function* (
      _question: string,
      _session: string,
      _context: string,
      options: { onRunStarted?: (runId: string) => void } = {},
    ) {
      options.onRunStarted?.('run-runtime')
      const plan = [
        { id: 'ask', title: '确认条件', description: '向用户提问', toolNames: ['ask_user'] as string[], status: 'queued' as const },
        { id: 'query', title: '筛选样本', description: '查询真实车源', toolNames: ['recommend'] as string[], status: 'queued' as const },
      ]
      yield { kind: 'plan_update', seq: 1, plan }
      yield { kind: 'plan_update', seq: 1, plan }
      yield { kind: 'tool_started', seq: 2, activity: { id: 'call-ask', toolName: 'ask_user', status: 'running' as const, detail: '正在提问' } }
      yield {
        kind: 'input_required',
        seq: 3,
        pendingInput: {
          tool_call_id: 'call-ask',
          step: 1,
          questions: [{ id: 'budget', prompt: '预算？', options: [], allow_custom: true, required: true }],
        },
      }
      return null
    })
    adapter.submitRunInput.mockResolvedValue(undefined)
    adapter.resumeAnalysis.mockImplementation(async function* () {
      yield { kind: 'tool_completed', seq: 4, activity: { id: 'call-ask', toolName: 'ask_user', status: 'complete' as const, detail: '回答已处理' } }
      yield { kind: 'tool_started', seq: 5, activity: { id: 'call-query', toolName: 'recommend', status: 'running' as const, detail: '查询中' } }
      yield { kind: 'tool_completed', seq: 6, activity: { id: 'call-query', toolName: 'recommend', status: 'complete' as const, detail: '返回样本' } }
      return report
    })

    await flow.submit('帮我选车')
    expect(flow.runtimeEvents.value).toHaveLength(3)
    expect(flow.runtime.value.plan.map((step) => step.status)).toEqual(['queued', 'queued'])
    expect(flow.runtime.value.activities[0]).toMatchObject({ status: 'waiting' })

    await flow.submitInput([{ question_id: 'budget', value: '15' }])

    expect(flow.runtime.value.plan.map((step) => step.status)).toEqual(['queued', 'queued'])
    expect(flow.runtime.value.activities).toEqual(expect.arrayContaining([
      expect.objectContaining({ id: 'call-ask', status: 'complete', resolution: 'handled' }),
      expect.objectContaining({ id: 'call-query', status: 'complete', resolution: 'handled' }),
    ]))
  })

  it('closes the answered ask_user activity from the backend completion event before the next tool runs', async () => {
    const flow = useAnalysisFlow()
    adapter.confirmAnalysis.mockImplementation(async function* (
      _question: string,
      _session: string,
      _context: string,
      options: { onRunStarted?: (runId: string) => void } = {},
    ) {
      options.onRunStarted?.('run-ask-completion')
      yield {
        kind: 'plan_update',
        seq: 1,
        plan: [
          { id: 'ask', title: '确认条件', description: '向用户提问', toolNames: ['ask_user'] as string[], status: 'queued' as const },
          { id: 'query', title: '查询样本', description: '继续调用数据工具', toolNames: ['recommend'] as string[], status: 'queued' as const },
        ],
      }
      yield { kind: 'tool_started', seq: 2, activity: { id: 'call-ask', toolName: 'ask_user', status: 'running' as const, detail: '正在提问' } }
      yield {
        kind: 'input_required',
        seq: 3,
        pendingInput: {
          tool_call_id: 'call-ask',
          step: 1,
          questions: [{ id: 'budget', prompt: '预算？', options: [], allow_custom: true, required: true }],
        },
      }
      return null
    })
    adapter.submitRunInput.mockResolvedValue(undefined)
    adapter.resumeAnalysis.mockImplementation(async function* () {
      yield { kind: 'tool_completed', seq: 4, activity: { id: 'call-ask', toolName: 'ask_user', status: 'complete' as const, detail: '回答已收到' } }
      yield { kind: 'tool_started', seq: 5, activity: { id: 'call-query', toolName: 'recommend', status: 'running' as const, detail: '查询中' } }
      return null
    })

    await flow.submit('帮我选车')
    await flow.submitInput([{ question_id: 'budget', value: '15' }])

    expect(flow.runtime.value.activities).toEqual(expect.arrayContaining([
      expect.objectContaining({ id: 'call-ask', status: 'complete', resolution: 'handled' }),
      expect.objectContaining({ id: 'call-query', status: 'running' }),
    ]))
    expect(flow.runtime.value.plan.map((step) => step.status)).toEqual(['queued', 'queued'])
  })

  it('assigns repeated tool names to plan steps in call order', async () => {
    const flow = useAnalysisFlow()
    adapter.confirmAnalysis.mockImplementation(async function* (
      _question: string,
      _session: string,
      _context: string,
      options: { onRunStarted?: (runId: string) => void } = {},
    ) {
      options.onRunStarted?.('run-repeated-tool')
      yield {
        kind: 'plan_update',
        seq: 1,
        plan: [
          { id: 'scope', title: '确认筛选口径', description: '确认条件', toolNames: ['recommend'], status: 'running' as const },
          { id: 'recommend', title: '汇总推荐结果', description: '获取结果', toolNames: ['recommend'], status: 'queued' as const },
        ],
      }
      yield { kind: 'tool_started', seq: 2, activity: { id: 'call-recommend-1', toolName: 'recommend', planStepId: 'scope', status: 'running' as const, detail: '第一次查询' } }
      yield { kind: 'tool_completed', seq: 3, activity: { id: 'call-recommend-1', toolName: 'recommend', planStepId: 'scope', status: 'complete' as const, detail: '第一次返回' } }
      yield {
        kind: 'input_required',
        seq: 4,
        pendingInput: {
          tool_call_id: 'call-ask',
          step: 1,
          questions: [{ id: 'confirm', prompt: '确认？', options: [], allow_custom: true, required: true }],
        },
      }
      return null
    })
    adapter.submitRunInput.mockResolvedValue(undefined)
    adapter.resumeAnalysis.mockImplementation(async function* () {
      yield {
        kind: 'plan_update',
        seq: 5,
        plan: [
          { id: 'scope', title: '确认筛选口径', description: '确认条件', toolNames: ['recommend'], status: 'complete' as const },
          { id: 'recommend', title: '汇总推荐结果', description: '获取结果', toolNames: ['recommend'], status: 'running' as const },
        ],
      }
      yield { kind: 'tool_started', seq: 6, activity: { id: 'call-recommend-2', toolName: 'recommend', planStepId: 'recommend', status: 'running' as const, detail: '第二次查询' } }
      yield { kind: 'tool_completed', seq: 7, activity: { id: 'call-recommend-2', toolName: 'recommend', planStepId: 'recommend', status: 'complete' as const, detail: '第二次返回' } }
      return report
    })

    await flow.submit('帮我选车')

    expect(flow.runtime.value.plan).toEqual(expect.arrayContaining([
      expect.objectContaining({ id: 'scope', status: 'running' }),
      expect.objectContaining({ id: 'recommend', status: 'queued' }),
    ]))
    expect(flow.runtime.value.activities).toEqual(expect.arrayContaining([
      expect.objectContaining({ id: 'call-recommend-1', planStepId: 'scope' }),
    ]))

    await flow.submitInput([{ question_id: 'confirm', value: '继续' }])

    expect(flow.runtime.value.activities).toEqual(expect.arrayContaining([
      expect.objectContaining({ id: 'call-recommend-2', planStepId: 'recommend' }),
    ]))
    expect(flow.runtime.value.plan).toEqual(expect.arrayContaining([
      expect.objectContaining({ id: 'scope', status: 'complete' }),
      expect.objectContaining({ id: 'recommend', status: 'running' }),
    ]))
  })

  it('marks an unbound final plan step as running while waiting for the final answer', async () => {
    const flow = useAnalysisFlow()
    let releaseFinalAnswer!: () => void
    const finalAnswerGate = new Promise<void>((resolve) => { releaseFinalAnswer = resolve })
    adapter.confirmAnalysis.mockImplementation(async function* (
      _question: string,
      _session: string,
      _context: string,
      options: { onRunStarted?: (runId: string) => void } = {},
    ) {
      options.onRunStarted?.('run-finalizing')
      yield {
        kind: 'plan_update',
        seq: 1,
        plan: [
          { id: 'scope', title: '确认筛选口径', description: '确认条件', toolNames: ['ask_user'], status: 'complete' as const },
          { id: 'recommend', title: '汇总推荐结果', description: '查询结果', toolNames: ['recommend'], status: 'complete' as const },
          { id: 'final', title: '输出推荐结果与样本说明', description: '整理最终答案', toolNames: [], status: 'running' as const },
        ],
      }
      yield { kind: 'tool_completed', seq: 2, activity: { id: 'call-ask', toolName: 'ask_user', status: 'complete' as const, detail: '回答已处理' } }
      yield { kind: 'tool_completed', seq: 3, activity: { id: 'call-recommend', toolName: 'recommend', status: 'complete' as const, detail: '已返回结果' } }
      await finalAnswerGate
      return report
    })

    const pending = flow.submit('帮我选车')
    for (let index = 0; index < 10 && flow.runtime.value.activities.length < 2; index += 1) await Promise.resolve()

    expect(flow.runtime.value.plan).toEqual(expect.arrayContaining([
      expect.objectContaining({ id: 'scope', status: 'complete' }),
      expect.objectContaining({ id: 'recommend', status: 'complete' }),
      expect.objectContaining({ id: 'final', status: 'running' }),
    ]))

    releaseFinalAnswer()
    await pending
    expect(flow.runtime.value.plan.map((step) => step.status)).toEqual(['complete', 'complete', 'running'])
    expect(flow.phase.value).toBe('complete')
    expect(flow.runtime.value.status).toBe('done')
  })

  it('adds a visible waiting plan when input arrives before any plan update', async () => {
    const flow = useAnalysisFlow()
    adapter.confirmAnalysis.mockImplementation(async function* (
      _question: string,
      _session: string,
      _context: string,
      options: { onRunStarted?: (runId: string) => void } = {},
    ) {
      options.onRunStarted?.('run-plan-late')
      yield {
        kind: 'input_required',
        seq: 3,
        pendingInput: {
          tool_call_id: 'call-ask-late',
          step: 1,
          questions: [{ id: 'budget', prompt: '预算？', options: [], allow_custom: true, required: true }],
        },
      }
      return null
    })

    await flow.submit('帮我选车')

    expect(flow.runtime.value.plan).toEqual([])
    expect(flow.runtime.value.waitState).toMatchObject({ status: 'waiting_user', toolCallId: 'call-ask-late' })
  })

  it('keeps a waiting plan visible when a completed plan receives a new question', async () => {
    const flow = useAnalysisFlow()
    adapter.confirmAnalysis.mockImplementation(async function* (
      _question: string,
      _session: string,
      _context: string,
      options: { onRunStarted?: (runId: string) => void } = {},
    ) {
      options.onRunStarted?.('run-plan-followup')
      yield {
        kind: 'plan_update',
        seq: 1,
        plan: [{ id: 'query', title: '汇总推荐结果', description: '汇总工具结果', toolNames: ['recommend'], status: 'complete' as const }],
      }
      yield {
        kind: 'input_required',
        seq: 2,
        pendingInput: {
          tool_call_id: 'call-ask-followup',
          step: 3,
          questions: [{ id: 'next', prompt: '怎么处理？', options: [], allow_custom: true, required: true }],
        },
      }
      return null
    })

    await flow.submit('帮我选车')

    expect(flow.runtime.value.plan).toEqual([
      expect.objectContaining({ id: 'query', status: 'complete' }),
    ])
    expect(flow.runtime.value.waitState).toMatchObject({ status: 'waiting_user', toolCallId: 'call-ask-followup' })
  })

  it('keeps the question card recoverable when answer submission fails', async () => {
    const flow = useAnalysisFlow()
    adapter.confirmAnalysis.mockImplementation(async function* (
      _question: string,
      _session: string,
      _context: string,
      options: { onRunStarted?: (runId: string) => void } = {},
    ) {
      options.onRunStarted?.('run-recoverable')
      yield {
        kind: 'input_required',
        seq: 2,
        pendingInput: {
          tool_call_id: 'call-ask',
          step: 1,
          questions: [{ id: 'budget', prompt: '预算？', options: [], allow_custom: true, required: true }],
        },
      }
      return null
    })
    adapter.submitRunInput.mockRejectedValue(new Error('服务暂时不可用'))

    await flow.submit('帮我选车')
    await flow.submitInput([{ question_id: 'budget', value: '15' }])

    expect(flow.phase.value).toBe('awaiting_input')
    expect(flow.runtime.value.pendingInput?.tool_call_id).toBe('call-ask')
    expect(flow.runtime.value.status).toBe('waiting_user')
    expect(flow.error.value).toContain('服务暂时不可用')
  })

  it('atomically closes the runtime when resume returns a report from the snapshot watchdog', async () => {
    const flow = useAnalysisFlow()
    adapter.confirmAnalysis.mockImplementation(async function* (
      _question: string,
      _session: string,
      _context: string,
      options: { onRunStarted?: (runId: string) => void } = {},
    ) {
      options.onRunStarted?.('run-watchdog-flow')
      yield {
        kind: 'plan_update',
        seq: 1,
        plan: [{ id: 'query', title: '查询样本', description: '读取数据', toolNames: ['recommend'], status: 'complete' as const }],
      }
      yield { kind: 'input_required', seq: 2, pendingInput: {
        tool_call_id: 'call-ask',
        step: 1,
        questions: [{ id: 'budget', prompt: '预算？', options: [], allow_custom: true, required: true }],
      } }
      return null
    })
    adapter.submitRunInput.mockResolvedValue(undefined)
    adapter.resumeAnalysis.mockImplementation(async function* () {
      yield { kind: 'tool_completed', seq: 3, activity: { id: 'call-query', toolName: 'recommend', status: 'complete' as const, detail: '已返回' } }
      return { ...report, id: 'run-watchdog-flow' }
    })

    await flow.submit('帮我选车')
    await flow.submitInput([{ question_id: 'budget', value: '15' }])

    expect(flow.phase.value).toBe('complete')
    expect(flow.reportId.value).toBe('run-watchdog-flow')
    expect(flow.runtime.value.status).toBe('done')
    expect(flow.runtime.value.pendingInput).toBeNull()
    expect(flow.runtime.value.plan.every((step) => step.status === 'complete')).toBe(true)
  })

  it('does not leave the page running when resume ends without a report or new question', async () => {
    const flow = useAnalysisFlow()
    adapter.confirmAnalysis.mockImplementation(async function* (
      _question: string,
      _session: string,
      _context: string,
      options: { onRunStarted?: (runId: string) => void } = {},
    ) {
      options.onRunStarted?.('run-empty-resume')
      yield {
        kind: 'input_required',
        seq: 2,
        pendingInput: {
          tool_call_id: 'call-empty-resume',
          step: 1,
          questions: [{ id: 'budget', prompt: '预算？', options: [], allow_custom: true, required: true }],
        },
      }
      return null
    })
    adapter.submitRunInput.mockResolvedValue(undefined)
    adapter.resumeAnalysis.mockImplementation(async function* () {
      return null
    })

    await flow.submit('帮我选车')
    await flow.submitInput([{ question_id: 'budget', value: '15' }])

    expect(flow.phase.value).toBe('error')
    expect(flow.error.value).toContain('没有返回最终结果')
  })

  it('keeps the agent message when no structured report is returned', async () => {
    adapter.confirmAnalysis.mockImplementation(async function* () {
      throw new Error('Agent 需要更多条件')
    })
    const flow = useAnalysisFlow()

    await flow.submit(draft.question)

    expect(flow.phase.value).toBe('error')
    expect(flow.error.value).toContain('Agent 需要更多条件')
  })

  it('uses the backend retry endpoint instead of rebuilding the old draft', async () => {
    const flow = useAnalysisFlow()
    adapter.confirmAnalysis.mockImplementation(async function* (
      _value: string,
      _sessionId: string,
      _contextId: string,
      options: { onRunStarted?: (runId: string) => void } = {},
    ) {
      options.onRunStarted?.('run-1')
      throw new AnalysisAdapterError('临时服务错误', 'backend_error', 'temporary_failure', true)
    })
    adapter.retryRun.mockResolvedValue({ run_id: 'run-retry', session_id: 'session-test', context_id: 'context-retry', status: 'running' })
    adapter.resumeAnalysis.mockImplementation(async function* () {
      return { ...report, id: 'run-retry' }
    })

    await flow.submit(draft.question)
    await flow.retry()

    expect(adapter.retryRun).toHaveBeenCalledWith('run-1', expect.any(AbortSignal))
    expect(flow.runId.value).toBe('run-retry')
    expect(flow.contextId.value).toBe('context-retry')
    expect(adapter.confirmAnalysis).toHaveBeenCalledTimes(1)
  })

  it('hydrates a waiting run and applies a mid-run correction using its revision', async () => {
    const flow = useAnalysisFlow()
    const pendingInput = {
      tool_call_id: 'call-ask',
      step: 2,
      resumeRevision: 7,
      questions: [{ id: 'budget', prompt: '预算？', options: [], allow_custom: true, required: true }],
    }
    const plan = [{ id: 'query', title: '查询车源', description: '筛选样本', toolNames: ['recommend'], status: 'complete' as const }]
    adapter.getRun.mockResolvedValue({
      run_id: 'run-existing',
      session_id: 'session-test',
      context_id: 'context-existing',
      input: '帮我找一辆 SUV',
      status: 'waiting_user',
      last_event_seq: 17,
      input_revision: 3,
      capabilities: { midrun_input: true, recover: true },
      pending_input: pendingInput,
    })
    adapter.runtimeFromSnapshot.mockReturnValue({
      plan,
      activities: [{ id: 'call-ask', toolName: 'ask_user', status: 'waiting', detail: '等待回答' }],
      pendingInput,
      planRevision: 2,
    })
    adapter.submitRunMessage.mockResolvedValue({ revision: 4, status: 'received' })
    adapter.resumeAnalysis.mockImplementation(async function* (
      _runId: string,
      options: { afterSeq?: number; onEvent?: (seq: number) => void } = {},
    ) {
      expect(options.afterSeq).toBe(17)
      const clientMessageId = String(adapter.submitRunMessage.mock.calls.at(-1)?.[2])
      options.onEvent?.(19)
      yield { kind: 'message_received', seq: 18, clientMessageId, revision: 4 }
      yield { kind: 'message_applied', seq: 19, clientMessageId, revision: 5 }
      return { ...report, id: 'run-existing' }
    })

    await flow.attachRun('run-existing')
    expect(flow.phase.value).toBe('awaiting_input')
    expect(flow.contextId.value).toBe('context-existing')
    expect(flow.lastEventSeq.value).toBe(17)
    expect(flow.runtime.value.planRevision).toBe(2)
    expect(flow.runtime.value.capabilities).toEqual({ midrunInput: true, recover: true })

    await flow.submitCorrection('预算改为 12 万以内')

    expect(adapter.submitRunMessage).toHaveBeenCalledWith(
      'run-existing',
      '预算改为 12 万以内',
      expect.any(String),
      3,
      expect.any(AbortSignal),
    )
    expect(flow.phase.value).toBe('complete')
    expect(flow.runtime.value.inputRevision).toBe(5)
    expect(flow.runtimeEvents.value.map((event) => event.kind)).toEqual(['message_received', 'message_applied'])
    expect(flow.correctionStatus.value).toBe('applied')
  })

  it('resumes a server-accepted waiting-run steering command without posting it twice', async () => {
    const flow = useAnalysisFlow('context-existing')
    const pendingInput = {
      tool_call_id: 'call-ask', step: 2, resumeRevision: 4,
      questions: [{ id: 'city', prompt: '城市？', options: [], allow_custom: true, required: true, selection_mode: 'single', max_selections: 1 }],
    }
    adapter.getRun.mockResolvedValue({
      run_id: 'run-existing', session_id: 'session-test', context_id: 'context-existing',
      input: '帮我找一辆 SUV', status: 'waiting_user', last_event_seq: 17,
      capabilities: { midrun_input: true, recover: true }, pending_input: pendingInput,
    })
    adapter.runtimeFromSnapshot.mockReturnValue({ plan: [], activities: [], pendingInput })
    adapter.resumeAnalysis.mockImplementation(async function* (
      _runId: string,
      options: { afterSeq?: number } = {},
    ) {
      expect(options.afterSeq).toBe(17)
      return { ...report, id: 'run-existing' }
    })

    await flow.attachRun('run-existing')
    expect(flow.phase.value).toBe('awaiting_input')
    await flow.resumeAcceptedRun()

    expect(adapter.submitRunMessage).not.toHaveBeenCalled()
    expect(adapter.resumeAnalysis).toHaveBeenCalledWith('run-existing', expect.objectContaining({ afterSeq: 17 }))
    expect(flow.phase.value).toBe('complete')
  })

  it('submits a correction during an active run without opening a second event stream', async () => {
    const flow = useAnalysisFlow()
    let releaseRun!: () => void
    const runGate = new Promise<void>((resolve) => { releaseRun = resolve })
    adapter.confirmAnalysis.mockImplementation(async function* (
      _question: string,
      _session: string,
      _context: string,
      options: { onRunStarted?: (runId: string) => void; onRunAccepted?: (run: { capabilities?: { midrun_input?: boolean }; input_revision?: number }) => void } = {},
    ) {
      options.onRunStarted?.('run-active')
      options.onRunAccepted?.({ capabilities: { midrun_input: true }, input_revision: 6 })
      yield { kind: 'tool_started', seq: 2, activity: { id: 'call-query', toolName: 'recommend', status: 'running' as const } }
      await runGate
      return { ...report, id: 'run-active' }
    })
    adapter.submitRunMessage.mockResolvedValue({ revision: 7, status: 'accepted' })

    const activeRun = flow.submit('15 万以内推荐 SUV')
    for (let attempt = 0; attempt < 10 && flow.runtime.value.activities.length === 0; attempt += 1) await Promise.resolve()
    expect(flow.phase.value).toBe('running')
    await flow.submitCorrection('预算调整为 12 万以内')

    expect(adapter.submitRunMessage).toHaveBeenCalledWith('run-active', '预算调整为 12 万以内', expect.any(String), 6, expect.any(AbortSignal))
    expect(adapter.resumeAnalysis).not.toHaveBeenCalled()
    expect(flow.correctionStatus.value).toBe('received')

    releaseRun()
    await activeRun
    expect(flow.phase.value).toBe('complete')
  })
  it('reuses an unacknowledged creation command and starts fresh only after explicit new analysis', async () => {
    const calls: Array<{ payload: unknown; context: string; key?: string }> = []
    adapter.confirmAnalysis.mockImplementation(async function* (payload, _session, context, options) {
      calls.push({ payload, context, key: options.clientRequestId })
      throw new AnalysisAdapterError('响应丢失', 'network_error', undefined, true)
    })
    const flow = useAnalysisFlow()
    await flow.submit('同一问题')
    await flow.submit('同一问题')
    expect(calls[1]).toEqual(calls[0])
    expect(calls[0].key).toBeTruthy()
    await flow.submit('同一问题', { newAnalysis: true })
    expect(calls[2].key).not.toBe(calls[0].key)
    expect(adapter.createAnalysisContextId).toHaveBeenCalledTimes(2)
  })

  it('restores the event subscription when the cancel request fails after aborting it', async () => {
    const flow = useAnalysisFlow()
    flow.runId.value = 'run-cancel-recovery'
    flow.phase.value = 'running'
    flow.runtime.value.status = 'running'
    adapter.cancelRun.mockRejectedValueOnce(new Error('取消接口拒绝请求'))
    const snapshot = { run_id: 'run-cancel-recovery', status: 'running', last_event_seq: 4 }
    adapter.getRun.mockResolvedValueOnce(snapshot)
    adapter.runtimeFromSnapshot.mockReturnValue({ plan: [], activities: [], pendingInput: null })
    let finish!: () => void
    const pending = new Promise<void>((resolve) => { finish = resolve })
    adapter.resumeAnalysis.mockImplementation(async function* (_id, options) {
      options.onSnapshot?.(snapshot)
      await pending
      return report
    })

    const cancellation = flow.cancel()
    await vi.waitFor(() => expect(adapter.resumeAnalysis).toHaveBeenCalledWith('run-cancel-recovery', expect.objectContaining({ afterSeq: 4 })))
    expect(flow.phase.value).toBe('running')
    expect(flow.failureCode.value).toBe('cancel_request_failed')
    expect(flow.error.value).toContain('取消接口拒绝请求')
    finish()
    await cancellation
    expect(flow.phase.value).toBe('complete')
  })

  it('evicts the least recently used terminal conversation flow after the cache limit', () => {
    const firstId = `cache-${Date.now()}-0`
    const first = useAnalysisFlow(firstId)
    first.phase.value = 'complete'
    for (let index = 1; index <= 16; index += 1) {
      const flow = useAnalysisFlow(`${firstId}-${index}`)
      flow.phase.value = 'complete'
    }
    expect(useAnalysisFlow(firstId)).not.toBe(first)
  })

  it('keeps a page-retained terminal flow coherent until release, then disposes it on eviction', () => {
    const firstId = `retained-cache-${Date.now()}`
    const retained = retainAnalysisFlow(firstId)
    retained.flow.phase.value = 'complete'
    for (let index = 1; index <= 16; index += 1) {
      const flow = useAnalysisFlow(`${firstId}-${index}`)
      flow.phase.value = 'complete'
    }
    expect(useAnalysisFlow(firstId)).toBe(retained.flow)

    retained.release()
    for (let index = 17; index <= 33; index += 1) {
      const flow = useAnalysisFlow(`${firstId}-${index}`)
      flow.phase.value = 'complete'
    }
    expect(useAnalysisFlow(firstId)).not.toBe(retained.flow)
  })

  it('aborts an outstanding request when an unretained terminal flow is evicted', async () => {
    const firstId = `disposed-cache-${Date.now()}`
    const flow = useAnalysisFlow(firstId)
    let signal: AbortSignal | undefined
    let finishRequest!: (snapshot: { run_id: string; status: string }) => void
    adapter.getRun.mockImplementationOnce((_id, requestSignal) => {
      signal = requestSignal
      return new Promise((resolve) => { finishRequest = resolve })
    })
    const attaching = flow.attachRun('pending-eviction')
    await Promise.resolve()
    flow.phase.value = 'complete'
    for (let index = 1; index <= 17; index += 1) {
      const item = useAnalysisFlow(`${firstId}-${index}`)
      item.phase.value = 'complete'
    }
    expect(signal?.aborted).toBe(true)
    finishRequest({ run_id: 'pending-eviction', status: 'complete' })
    await attaching
  })

  it('reuses the answer command after a lost response without changing its payload', async () => {
    const flow = useAnalysisFlow()
    flow.runId.value = 'answer-lost'
    flow.runtime.value.pendingInput = {
      tool_call_id: 'ask-lost', step: 1, resumeRevision: 7,
      questions: [{ id: 'budget', prompt: '预算', options: [], allow_custom: true, required: true, selection_mode: 'single', max_selections: 1 }],
    }
    adapter.submitRunInput.mockRejectedValueOnce(new AnalysisAdapterError('response lost', 'network_error', undefined, true))
      .mockResolvedValueOnce({ accepted_tool_call_id: 'ask-lost' })
    adapter.resumeAnalysis.mockImplementation(async function* () { return report })
    const answers = [{ question_id: 'budget', values: ['15'] }]
    await flow.submitInput(answers)
    await flow.submitInput(answers)
    expect(adapter.submitRunInput.mock.calls[1]?.[4]).toBe(adapter.submitRunInput.mock.calls[0]?.[4])
    expect(adapter.submitRunInput.mock.calls[1]?.[1]).toEqual(adapter.submitRunInput.mock.calls[0]?.[1])
    expect(flow.phase.value).toBe('complete')
  })

})
