import { describe, expect, it } from 'vitest'
import { reduceRunEvent, reduceRunSnapshot, type RunReducerState } from './runReducer'

const initial: RunReducerState = {
  runtime: { plan: [], activities: [], pendingInput: null, waitState: null, status: 'running' },
  progress: [],
  runtimeEvents: [],
  phase: 'running',
  correctionStatus: 'idle',
  correctionError: '',
  isSubmitting: true,
}

describe('run state reducer', () => {
  it('applies the authoritative task intent event before choosing the plan display mode', () => {
    const next = reduceRunEvent(initial, {
      kind: 'task_intent_update',
      seq: 1,
      taskMode: 'simple',
      revision: 1,
      taskGoals: [{ id: 'query', description: '查询宝马3系价格', deliverable: '价格分布', dependsOn: [] }],
    }, { hasPendingCorrection: false })

    expect(next.runtime.taskMode).toBe('simple')
    expect(next.runtime.taskGoals?.[0].deliverable).toBe('价格分布')
    expect(next.runtime.taskIntentRevision).toBe(1)
  })

  it('applies a waiting event as one runtime transition and closes a stale submission', () => {
    const next = reduceRunEvent(initial, {
      kind: 'input_required',
      seq: 7,
      pendingInput: {
        tool_call_id: 'call-new',
        step: 2,
        questions: [],
      },
    }, { submittedToolCallId: 'call-old', hasPendingCorrection: false })

    expect(next.phase).toBe('awaiting_input')
    expect(next.runtime.status).toBe('waiting_user')
    expect(next.runtime.pendingInput?.tool_call_id).toBe('call-new')
    expect(next.runtime.activities[0].status).toBe('waiting')
    expect(next.isSubmitting).toBe(false)
    expect(next.runtimeEvents).toHaveLength(1)
  })

  it('does not apply or append the same sequenced event twice', () => {
    const event = { kind: 'message_applied' as const, seq: 9, clientMessageId: 'msg-1', revision: 3 }
    const once = reduceRunEvent(initial, event, { hasPendingCorrection: false })
    const twice = reduceRunEvent(once, event, { hasPendingCorrection: false })

    expect(twice).toBe(once)
    expect(twice.runtime.inputRevision).toBe(3)
    expect(twice.runtimeEvents).toHaveLength(1)
  })

  it('bounds retained event history while advancing the deduplication cursor', () => {
    let state = initial
    for (let seq = 1; seq <= 500; seq += 1) {
      state = reduceRunEvent(state, { kind: 'legacy_progress', seq, phase: 'understanding', status: 'running', label: `第 ${seq} 步`, detail: '', percent: 1 }, { hasPendingCorrection: false })
    }
    expect(state.runtimeEvents).toHaveLength(200)
    expect(state.progress).toHaveLength(200)
    expect(state.runtimeEvents[0]).toMatchObject({ seq: 301 })
    expect(state.lastEventSeq).toBe(500)
    expect(reduceRunEvent(state, { kind: 'legacy_progress', seq: 1, phase: 'understanding', status: 'running', label: '重复', detail: '', percent: 1 }, { hasPendingCorrection: false })).toBe(state)
  })

  it('hydrates a waiting snapshot through the reducer and rejects older snapshots', () => {
    const waiting = reduceRunSnapshot(initial, {
      status: 'waiting_user',
      attempt: 2,
      stateRevision: 8,
      lastEventSeq: 12,
      runtime: {
        plan: [],
        activities: [],
        pendingInput: { tool_call_id: 'ask-2', step: 1, questions: [] },
        waitState: null,
        status: 'waiting_user',
      },
    })

    expect(waiting.phase).toBe('awaiting_input')
    expect(waiting.runtime.waitState?.toolCallId).toBe('ask-2')
    expect(waiting.lastEventSeq).toBe(12)

    const older = reduceRunSnapshot(waiting, {
      status: 'running',
      attempt: 1,
      stateRevision: 99,
      lastEventSeq: 99,
      runtime: { ...initial.runtime, status: 'running' },
    })
    expect(older).toBe(waiting)
  })

  it('accepts a low revision from a newer attempt and rejects its late old attempt event', () => {
    const current = reduceRunSnapshot(initial, {
      runId: 'run-1', status: 'running', attempt: 3, stateRevision: 90, lastEventSeq: 90,
      runtime: { ...initial.runtime, plan: [{ id: 'old', title: '旧', description: '', toolNames: [], status: 'complete' }], planRevision: 8 },
    })
    const old = reduceRunEvent(current, {
      kind: 'plan_update', seq: 91, attempt: 2, stateRevision: 99, revision: 99,
      plan: [{ id: 'late', title: '迟到', description: '', toolNames: [], status: 'running' }],
    }, { hasPendingCorrection: false })
    expect(old).toBe(current)
    const next = reduceRunEvent(current, {
      kind: 'plan_update', seq: 1, attempt: 4, stateRevision: 1, revision: 1,
      plan: [{ id: 'new', title: '新尝试', description: '', toolNames: [], status: 'running' }],
    }, { hasPendingCorrection: false })
    expect(next.runtime.plan[0].id).toBe('new')
    expect(next.runtime.planRevision).toBe(1)
  })

  it('does not carry an old attempt plan into a newer attempt snapshot', () => {
    const current = reduceRunSnapshot(initial, {
      runId: 'run-retry', status: 'running', attempt: 2, stateRevision: 90, lastEventSeq: 95,
      runtime: {
        ...initial.runtime,
        plan: [{ id: 'old', title: '旧计划', description: '', toolNames: [], status: 'complete' }],
        planRevision: 8,
      },
    })
    const next = reduceRunSnapshot(current, {
      runId: 'run-retry', status: 'running', attempt: 3, stateRevision: 1, lastEventSeq: 98,
      runtime: { ...initial.runtime, plan: [], planRevision: 0 },
    })

    expect(next.runtime.plan).toEqual([])
    expect(next.runtime.planRevision).toBe(0)
    expect(next.attempt).toBe(3)
  })

  it('does not erase a known pending question when a partial running snapshot arrives', () => {
    const waiting = reduceRunSnapshot(initial, {
      runId: 'run-2', status: 'waiting_user', attempt: 1, stateRevision: 2,
      runtime: { ...initial.runtime, pendingInput: { tool_call_id: 'ask-1', step: 1, questions: [] } },
    })
    const partial = reduceRunSnapshot(waiting, {
      runId: 'run-2', status: 'running', attempt: 1, stateRevision: 3,
      runtime: { ...initial.runtime, plan: [], activities: [], pendingInput: null },
    })
    expect(partial.runtime.pendingInput?.tool_call_id).toBe('ask-1')
  })

  it('uses a complete snapshot to clear answered input and confirm its coverage cursor', () => {
    const waiting = reduceRunSnapshot(initial, {
      runId: 'run-answer', status: 'waiting_user', attempt: 1, stateRevision: 2,
      runtime: { ...initial.runtime, pendingInput: { tool_call_id: 'ask-old', step: 1, questions: [] } },
    })
    const resumed = reduceRunSnapshot(waiting, {
      runId: 'run-answer', status: 'running', attempt: 1, stateRevision: 4, lastEventSeq: 14,
      coveredEventSeq: 13,
      runtime: { ...initial.runtime, pendingInput: null, status: 'running' },
      completeSnapshot: true,
    })

    expect(resumed.runtime.pendingInput).toBeNull()
    expect(resumed.runtime.waitState).toBeNull()
    expect(resumed.snapshotCoveredSeq).toBe(13)
  })
})
