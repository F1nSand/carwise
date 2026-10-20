import type { AgentPlanStep, PlanUpdateRuntimeEvent } from '../types/analysis'

export function planIsSettled(plan: AgentPlanStep[]): boolean {
  return plan.every((step) => step.status === 'complete' || step.status === 'skipped')
}

function sameDefinition(left: AgentPlanStep, right: AgentPlanStep): boolean {
  return left.id === right.id && left.title === right.title && left.description === right.description
    && left.toolNames.length === right.toolNames.length
    && left.toolNames.every((tool, index) => tool === right.toolNames[index])
}

export function validPlanUpdate(previous: AgentPlanStep[], revision: number, event: PlanUpdateRuntimeEvent): boolean {
  const next = event.plan
  if (!next.length || new Set(next.map((step) => step.id)).size !== next.length) return false
  if (next.some((step) => !step.id || !['queued', 'running', 'complete', 'skipped'].includes(step.status))) return false
  if (next.some((step) => step.status === 'skipped' && !step.reason?.trim())) return false
  if (next.filter((step) => step.status === 'running').length > 1) return false
  if (!next.every((step, index) => step.status === 'queued' || planIsSettled(next.slice(0, index)))) return false
  if (event.revision !== undefined && (!Number.isInteger(event.revision) || event.revision < revision)) return false
  const revised = event.revision !== undefined && event.revision > revision && Boolean(event.reason?.trim())
  if (!previous.length) return true
  if (revised) {
    // Completed evidence-bearing work cannot be rewritten by a new plan.
    return previous.filter((step) => step.status === 'complete' || step.status === 'skipped')
      .every((step, index) => next[index] !== undefined && sameDefinition(step, next[index])
        && step.status === next[index].status && step.reason === next[index].reason)
  }
  const rank = { queued: 0, running: 1, complete: 2, skipped: 2 }
  return previous.length === next.length && previous.every((step, index) => {
    const following = next[index]
    return following !== undefined && sameDefinition(step, following)
      && rank[following.status] >= rank[step.status]
      && (!(step.status === 'complete' || step.status === 'skipped') || following.status === step.status)
  })
}
