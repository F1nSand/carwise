import { describe, expect, it } from 'vitest'
import type { AgentPlanStep, PlanUpdateRuntimeEvent } from '../types/analysis'
import { planIsSettled, validPlanUpdate } from './planState'

const step = (id: string, status: AgentPlanStep['status'], reason?: string): AgentPlanStep => ({
  id, title: id, description: '查询条件', toolNames: ['csv_query'], status, reason,
})
const event = (plan: AgentPlanStep[], revision?: number, reason?: string): PlanUpdateRuntimeEvent => ({
  kind: 'plan_update', seq: 1, plan, revision, reason,
})

describe('plan updates', () => {
  it('allows direct answers and explicitly skipped steps to settle', () => {
    expect(planIsSettled([])).toBe(true)
    expect(planIsSettled([step('one', 'complete'), step('two', 'skipped', '没有匹配样本')])).toBe(true)
    expect(planIsSettled([step('one', 'running')])).toBe(false)
  })
  it('requires a reason for skipped work and preserves completed work', () => {
    expect(validPlanUpdate([], 0, event([step('one', 'skipped')]))).toBe(false)
    const previous = [step('one', 'complete'), step('two', 'queued')]
    expect(validPlanUpdate(previous, 1, event([step('one', 'complete'), step('new', 'running')], 2, '用户更改城市'))).toBe(true)
    expect(validPlanUpdate(previous, 1, event([step('changed', 'complete'), step('new', 'running')], 2, '更改'))).toBe(false)
  })
  it('rejects stale revisions, unannounced rewrites, and state rollback', () => {
    const previous = [step('one', 'running')]
    expect(validPlanUpdate(previous, 2, event([step('one', 'complete')], 1))).toBe(false)
    expect(validPlanUpdate(previous, 2, event([step('new', 'running')], 3))).toBe(false)
    expect(validPlanUpdate(previous, 2, event([step('one', 'queued')], 2))).toBe(false)
  })
  it('accepts ordered progress after a skipped predecessor', () => {
    expect(validPlanUpdate([], 0, event([step('one', 'skipped', '无样本'), step('two', 'running')]))).toBe(true)
    expect(validPlanUpdate([], 0, event([step('one', 'queued'), step('two', 'running')]))).toBe(false)
  })
})
