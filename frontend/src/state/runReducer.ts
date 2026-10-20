import { validPlanUpdate } from '../utils/planState'
import type {
  AgentRuntimeState,
  AgentToolActivity,
  AnalysisPhase,
  AnalysisProgressEvent,
  AnalysisRuntimeEvent,
  CorrectionStatus,
} from '../types/analysis'

const MAX_VISIBLE_RUN_EVENTS = 200

function limitActivities(activities: AgentToolActivity[]): AgentToolActivity[] {
  return activities.length > MAX_VISIBLE_RUN_EVENTS ? activities.slice(-MAX_VISIBLE_RUN_EVENTS) : activities
}

export interface RunReducerState {
  runtime: AgentRuntimeState
  progress: AnalysisProgressEvent[]
  runtimeEvents: AnalysisRuntimeEvent[]
  phase: AnalysisPhase
  correctionStatus: CorrectionStatus
  correctionError: string
  isSubmitting: boolean
  attempt?: number
  stateRevision?: number
  lastEventSeq?: number
  runId?: string
  receivedEventSeq?: number
  appliedEventSeq?: number
  coveredEventSeq?: number
  snapshotCoveredSeq?: number
  completeSnapshot?: boolean
}

export interface RunSnapshotUpdate {
  runtime: AgentRuntimeState
  status: string
  attempt?: number
  stateRevision?: number
  lastEventSeq?: number
  runId?: string
  taskRevision?: number
  planRevision?: number
  receivedEventSeq?: number
  appliedEventSeq?: number
  coveredEventSeq?: number
  snapshotCoveredSeq?: number
}

export interface RunReducerContext {
  submittedToolCallId?: string
  pendingCorrectionClientMessageId?: string
  hasPendingCorrection: boolean
}

export function reduceRunSnapshot(
  current: RunReducerState,
  snapshot: RunSnapshotUpdate,
): RunReducerState {
  const newAttempt = current.attempt !== undefined
    && snapshot.attempt !== undefined
    && snapshot.attempt > current.attempt
  if (current.runId && snapshot.runId && current.runId !== snapshot.runId) return current
  if (current.attempt !== undefined && snapshot.attempt !== undefined) {
    if (snapshot.attempt < current.attempt) return current
    if (snapshot.attempt === current.attempt
      && current.stateRevision !== undefined
      && snapshot.stateRevision !== undefined
      && snapshot.stateRevision < current.stateRevision) return current
    if (snapshot.attempt === current.attempt
      && current.lastEventSeq !== undefined
      && snapshot.lastEventSeq !== undefined
      && snapshot.lastEventSeq < current.lastEventSeq) return current
  }

  const authoritativeSnapshot = snapshot.completeSnapshot === true
  const pendingInput = newAttempt || authoritativeSnapshot
    ? snapshot.runtime.pendingInput
    : snapshot.runtime.pendingInput || current.runtime.pendingInput
  const waiting = snapshot.status === 'waiting_user' && Boolean(pendingInput)
  const phase: AnalysisPhase = waiting
    ? 'awaiting_input'
    : snapshot.status === 'done'
      ? 'complete'
      : snapshot.status === 'error' || snapshot.status === 'cancelled'
        ? 'error'
        : 'running'
  return {
    ...current,
    runtimeEvents: newAttempt ? [] : current.runtimeEvents.slice(-MAX_VISIBLE_RUN_EVENTS),
    progress: current.progress.slice(-MAX_VISIBLE_RUN_EVENTS),
    runtime: {
      ...snapshot.runtime,
      // A partial runtime projection must not erase the last known plan or
      // pending input. Full snapshots still replace these fields below.
      plan: newAttempt || authoritativeSnapshot || snapshot.runtime.plan.length || !current.runtime.plan.length ? snapshot.runtime.plan : current.runtime.plan,
      activities: limitActivities(newAttempt || authoritativeSnapshot || snapshot.runtime.activities.length || !current.runtime.activities.length ? snapshot.runtime.activities : current.runtime.activities),
      taskGoals: newAttempt || authoritativeSnapshot || snapshot.runtime.taskGoals?.length || !current.runtime.taskGoals?.length ? snapshot.runtime.taskGoals : current.runtime.taskGoals,
      planRevision: snapshot.planRevision ?? snapshot.runtime.planRevision ?? (newAttempt ? undefined : current.runtime.planRevision),
      taskRevision: snapshot.taskRevision ?? snapshot.runtime.taskRevision ?? (newAttempt ? undefined : current.runtime.taskRevision),
      status: snapshot.status,
      pendingInput,
      waitState: waiting && pendingInput ? {
        status: 'waiting_user',
        toolCallId: pendingInput.tool_call_id,
        planStepId: pendingInput.planStepId,
      } : null,
    },
    phase,
    isSubmitting: waiting ? false : current.isSubmitting,
    attempt: snapshot.attempt ?? current.attempt,
    stateRevision: snapshot.stateRevision ?? current.stateRevision,
    lastEventSeq: snapshot.lastEventSeq ?? current.lastEventSeq,
    runId: snapshot.runId ?? current.runId,
    receivedEventSeq: snapshot.receivedEventSeq ?? current.receivedEventSeq,
    appliedEventSeq: snapshot.appliedEventSeq ?? current.appliedEventSeq,
    coveredEventSeq: snapshot.coveredEventSeq ?? current.coveredEventSeq,
    snapshotCoveredSeq: authoritativeSnapshot && snapshot.coveredEventSeq !== undefined
      ? Math.max(current.snapshotCoveredSeq ?? 0, snapshot.coveredEventSeq)
      : snapshot.snapshotCoveredSeq ?? current.snapshotCoveredSeq,
  }
}

export function reduceRunEvent(
  current: RunReducerState,
  event: AnalysisRuntimeEvent,
  context: RunReducerContext,
): RunReducerState {
  if (current.runId && 'runId' in event && event.runId && event.runId !== current.runId) return current
  if ('attempt' in event && event.attempt !== undefined && current.attempt !== undefined && event.attempt < current.attempt) return current
  if ('attempt' in event && event.attempt !== undefined && current.attempt !== undefined && event.attempt > current.attempt) {
    // A newer attempt is a new stream identity; its low sequence/revision is valid.
    current = {
      ...current,
      runtimeEvents: [],
      lastEventSeq: 0,
      stateRevision: undefined,
      attempt: event.attempt,
      runtime: { ...current.runtime, plan: [], planRevision: undefined, pendingInput: null, waitState: null },
    }
  }
  if ('seq' in event && Number.isInteger(event.seq) && event.seq > 0 && event.seq <= (current.lastEventSeq ?? 0)) return current
  const next: RunReducerState = {
    ...current,
    runtimeEvents: [...current.runtimeEvents.slice(-(MAX_VISIBLE_RUN_EVENTS - 1)), event],
    ...('seq' in event && Number.isInteger(event.seq) && event.seq > 0
      ? { lastEventSeq: Math.max(current.lastEventSeq ?? 0, event.seq) }
      : {}),
  }

  if (event.kind === 'task_intent_update') {
    next.runtime = {
      ...current.runtime,
      taskMode: event.taskMode,
      taskGoals: event.taskGoals,
      taskIntentRevision: event.revision,
      taskRevision: event.taskRevision ?? event.revision,
      status: 'running',
    }
  } else if (event.kind === 'plan_update') {
    const revision = event.planRevision ?? event.revision
    const validOrder = validPlanUpdate(current.runtime.plan, current.runtime.planRevision ?? 0, { ...event, revision })
    if (validOrder) {
      next.runtime = {
        ...current.runtime,
        plan: event.plan,
        planRevision: revision ?? current.runtime.planRevision,
        planChangeReason: event.reason ?? current.runtime.planChangeReason,
        status: 'running',
      }
    } else {
      next.runtime = {
        ...current.runtime,
        activities: limitActivities([...current.runtime.activities, {
          id: `protocol-plan-${event.seq}`,
          toolName: '计划协议',
          status: 'error',
          resolution: 'active',
          detail: '收到非法计划状态，已保留上一个有效计划',
          step: 0,
        }]),
      }
    }
  } else if (event.kind === 'tool_started' || event.kind === 'tool_completed') {
    const previousActivity = current.runtime.activities.find((item) => item.id === event.activity.id)
    const activities = current.runtime.activities
      .filter((item) => item.id !== event.activity.id)
      .map((item) => ({ ...item }))
    const activity: AgentToolActivity = {
      ...event.activity,
      planStepId: previousActivity?.planStepId || event.activity.planStepId,
      resolution: event.activity.status === 'error'
        ? 'active'
        : event.kind === 'tool_completed'
          ? 'handled'
          : 'active',
    }
    const nextActivities = limitActivities([...activities, activity])
    if (activity.status === 'complete' && activity.planStepId) {
      nextActivities.forEach((item) => {
        if (item.planStepId === activity.planStepId && item.status === 'error' && item.resolution === 'active') {
          item.resolution = 'handled'
          item.detail = '已由后续调用修正'
        }
      })
    }
    next.runtime = { ...current.runtime, activities: nextActivities, status: 'running' }
  } else if (event.kind === 'input_required') {
    const existing = current.runtime.activities.find((activity) => activity.id === event.pendingInput.tool_call_id)
    const waitingActivity: AgentToolActivity = {
      id: event.pendingInput.tool_call_id,
      toolName: existing?.toolName || 'ask_user',
      status: 'waiting',
      resolution: 'active',
      detail: '等待你的回答',
      output: existing?.output,
      step: event.pendingInput.step,
      planStepId: event.pendingInput.planStepId || existing?.planStepId,
    }
    next.runtime = {
      ...current.runtime,
      activities: limitActivities([...current.runtime.activities.filter((activity) => activity.id !== waitingActivity.id), waitingActivity]),
      pendingInput: event.pendingInput,
      waitState: {
        status: 'waiting_user',
        toolCallId: event.pendingInput.tool_call_id,
        planStepId: event.pendingInput.planStepId,
      },
      status: 'waiting_user',
    }
    next.phase = 'awaiting_input'
    if (context.submittedToolCallId && event.pendingInput.tool_call_id !== context.submittedToolCallId) {
      next.isSubmitting = false
    }
  } else if (event.kind === 'message_received' || event.kind === 'message_applied') {
    if (event.revision !== undefined) {
      next.runtime = {
        ...current.runtime,
        inputRevision: Math.max(current.runtime.inputRevision ?? 0, event.revision),
      }
    }
    if (context.pendingCorrectionClientMessageId === event.clientMessageId) {
      next.correctionStatus = event.kind === 'message_applied' ? 'applied' : 'received'
      if (event.kind === 'message_applied') next.correctionError = ''
    } else if (!context.hasPendingCorrection) {
      next.correctionStatus = event.kind === 'message_applied' ? 'applied' : 'received'
    }
  } else {
    next.progress = [...current.progress.slice(-(MAX_VISIBLE_RUN_EVENTS - 1)), event]
    if (event.kind === 'legacy_progress' && event.phase === 'reporting' && event.status === 'running') {
      next.runtime = { ...current.runtime, status: 'running' }
    }
  }

  return next
}
