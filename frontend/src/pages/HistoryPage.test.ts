import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import HistoryPage from './HistoryPage.vue'
import HistoryList from '../components/history/HistoryList.vue'
import type { AnalysisRecord } from '../types/analysis'

const adapter = vi.hoisted(() => ({
  getSessionId: vi.fn(() => 'session-history'),
  listHistory: vi.fn(),
  retryRun: vi.fn(),
  recoverRun: vi.fn(),
}))

vi.mock('../services/analysisAdapter', () => adapter)

const records: AnalysisRecord[] = [
  {
    id: 'run-new',
    question: '15 万以内、3 年内的 SUV 推荐几款',
    intent: 'recommend',
    scenarioLabel: '智能购车',
    createdAt: '2026-09-17T02:00:00Z',
    summary: '推荐结论',
    confidence: 'high',
    reportId: 'run-new',
    status: 'done',
  },
  {
    id: 'run-old',
    question: '2022 年的奥迪 A4L 大概什么价格',
    intent: 'price_compare',
    scenarioLabel: '车型行情',
    createdAt: '2026-09-17T01:00:00Z',
    summary: '行情结论',
    confidence: 'medium',
    reportId: 'run-old',
    status: 'done',
  },
]

describe('HistoryPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    adapter.listHistory.mockResolvedValue(records)
  })

  afterEach(() => vi.useRealTimers())

  it('renders records newest-first', async () => {
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [
        { path: '/', component: { template: '<div />' } },
        { path: '/history', component: HistoryPage },
      ],
    })
    await router.push('/history')
    await router.isReady()

    const wrapper = mount(HistoryPage, { global: { plugins: [router] } })
    await flushPromises()

    const text = wrapper.text()
    expect(text.indexOf('15 万以内、3 年内的 SUV 推荐几款')).toBeLessThan(text.indexOf('2022 年的奥迪 A4L 大概什么价格'))
    expect(adapter.listHistory).toHaveBeenCalledWith('session-history')
  })

  it('offers a link back to analysis in the empty state', () => {
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: '/', component: { template: '<div />' } }],
    })
    const wrapper = mount(HistoryList, { props: { records: [] }, global: { plugins: [router] } })
    expect(wrapper.text()).toContain('还没有分析记录')
    expect(wrapper.get('a[href="/"]').text()).toContain('开始一次分析')
  })

  it('emits retry for a failed run', async () => {
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/', component: { template: '<div />' } }] })
    const wrapper = mount(HistoryList, {
      props: {
        records: [{ ...records[0], id: 'run-failed', reportId: 'run-failed', status: 'error', retryable: true }],
      },
      global: { plugins: [router] },
    })

    await wrapper.get('button.open-button').trigger('click')
    expect(wrapper.emitted('retry')?.[0]).toEqual(['run-failed'])
  })

  it('routes a new retry to the live runtime page so its events remain attached', async () => {
    adapter.listHistory.mockResolvedValue([{ ...records[0], id: 'run-failed', reportId: 'run-failed', status: 'error', retryable: true }])
    adapter.retryRun.mockResolvedValue({ run_id: 'run-retry', session_id: 'session-history', status: 'running' })
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [
        { path: '/', component: { template: '<div />' } },
        { path: '/history', component: HistoryPage },
      ],
    })
    await router.push('/history')
    await router.isReady()
    const wrapper = mount(HistoryPage, { global: { plugins: [router] } })
    await flushPromises()
    await wrapper.get('button.open-button').trigger('click')
    await flushPromises()

    expect(router.currentRoute.value.fullPath).toBe('/?resume_run=run-retry')
  })
})
