import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import AgentRuntimePanel from './AgentRuntimePanel.vue'
import type { AgentRuntimeState } from '../../types/analysis'

describe('AgentRuntimePanel', () => {
  it('renders the Agent plan and actual tool activity as the primary status', () => {
    const runtime: AgentRuntimeState = {
      plan: [{ id: 'query', title: '查询可比样本', description: '按条件获取真实车源', toolNames: ['price_compare'], status: 'running' }],
      activities: [{ id: 'call-1', toolName: 'price_compare', status: 'complete', detail: '返回 42 条样本' }],
      pendingInput: null,
      status: 'running',
    }
    const wrapper = mount(AgentRuntimePanel, { props: { runtime } })

    expect(wrapper.text()).toContain('查询可比样本')
    expect(wrapper.text()).toContain('price_compare')
    expect(wrapper.text()).toContain('返回 42 条样本')
    expect(wrapper.text()).not.toContain('理解购车条件')
    expect(wrapper.text()).not.toContain('整理结论与图表')
  })

  it('keeps an empty plan in plan-generation state after a legacy simple declaration', () => {
    const wrapper = mount(AgentRuntimePanel, { props: { runtime: {
      plan: [],
      activities: [],
      pendingInput: null,
      status: 'running',
      taskMode: 'simple',
      taskGoals: [{ id: 'query', description: '查价格', deliverable: '价格分布', dependsOn: [] }],
    } } })

    expect(wrapper.text()).toContain('正在生成计划')
    expect(wrapper.text()).toContain('价格分布')
  })
})
