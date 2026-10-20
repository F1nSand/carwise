import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import ExecutionGroup from './ExecutionGroup.vue'

describe('ExecutionGroup', () => {
  it('keeps a completed run folded while retaining its plan and safe tool summary', () => {
    const wrapper = mount(ExecutionGroup, {
      props: {
        status: 'complete',
        summary: '本轮执行完成',
        plan: [{ id: 'price', title: '查询价格', description: '', toolNames: [], status: 'complete' }],
        activities: [{ id: 'call-1', toolName: 'price_stats', status: 'complete', detail: '已取得 261 条样本' }],
      },
    })

    expect(wrapper.find('details').attributes('open')).toBeUndefined()
    expect(wrapper.text()).toContain('1 项计划 · 1 项工具活动')
    expect(wrapper.find('.execution-group__indicator--complete').exists()).toBe(true)
    expect((wrapper.find('details').element as HTMLDetailsElement).open).toBe(false)
    expect(wrapper.text()).toContain('查询价格')

    const details = wrapper.find('details').element as HTMLDetailsElement
    details.open = true
    details.dispatchEvent(new Event('toggle'))
    return wrapper.vm.$nextTick().then(() => {
      expect(wrapper.emitted('update:open')).toEqual([[true]])
    })
  })

  it('surfaces a failed historical run as an error state', () => {
    const wrapper = mount(ExecutionGroup, {
      props: { status: 'error', summary: '本轮未能完成', activities: [] },
    })

    expect(wrapper.find('.execution-group__indicator--error').exists()).toBe(true)
    expect(wrapper.text()).toContain('本轮未能完成')
  })
})
