import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import { afterEach, describe, expect, it, vi } from 'vitest'
import ReportPage from './ReportPage.vue'
import { recommendationFixture } from '../mocks/analysisFixtures'

const adapter = vi.hoisted(() => ({ getReport: vi.fn() }))
vi.mock('../services/analysisAdapter', () => adapter)
// Page tests verify report composition; ChartCard/AnalysisChart tests exercise
// lifecycle explicitly. jsdom has no Canvas layout or drawing implementation.
vi.mock('echarts/core', () => ({
  use: vi.fn(),
  init: () => ({ setOption: vi.fn(), resize: vi.fn(), dispose: vi.fn() }),
}))
enableAutoUnmount(afterEach)

describe('ReportPage', () => {
  it('renders evidence and a chart table alternative', async () => {
    adapter.getReport.mockResolvedValue({
      ...recommendationFixture,
      id: 'run-real',
      summaryText: '## 重点\n\n| 车系 | 价格 |\n|---|---|\n| 宝马 | 10 万 |',
    })
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [
        { path: '/', name: 'chat', component: { template: '<div />' } },
        { path: '/chat/:id', name: 'conversation', component: { template: '<div />' } },
        { path: '/chat', name: 'conversations', component: { template: '<div />' } },
        { path: '/analysis/:id', component: ReportPage },
      ],
    })
    await router.push('/analysis/report-recommendation')
    await router.isReady()

    const wrapper = mount(ReportPage, { global: { plugins: [router] } })
    await flushPromises()

    expect(wrapper.text()).toContain('挂牌价')
    expect(wrapper.text()).toContain('样本量')
    expect(wrapper.text()).toContain('可靠性')
    expect(wrapper.text()).toContain('数据限制')
    expect(wrapper.find('.insight-card .markdown-content__table-wrap').exists()).toBe(true)
    expect(wrapper.get('button[aria-expanded]').text()).toContain('查看数据表')
    expect(adapter.getReport).toHaveBeenCalledWith('report-recommendation')
  })

  it('renders a direct Agent answer without requiring an evidence report', async () => {
    adapter.getReport.mockResolvedValue({
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
    })
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [
        { path: '/', name: 'chat', component: { template: '<div />' } },
        { path: '/chat/:id', name: 'conversation', component: { template: '<div />' } },
        { path: '/chat', name: 'conversations', component: { template: '<div />' } },
        { path: '/analysis/:id', component: ReportPage },
      ],
    })
    await router.push('/analysis/run-answer')
    await router.isReady()

    const wrapper = mount(ReportPage, { global: { plugins: [router] } })
    await flushPromises()

    expect(wrapper.text().match(/先看挂牌价和中位价。/g)).toHaveLength(1)
    expect(wrapper.text()).toContain('Agent 回答')
    expect(wrapper.find('.answer-highlight').exists()).toBe(false)
    expect(wrapper.findAll('.agent-answer-card')).toHaveLength(1)
    expect(wrapper.find('.trust-rail').text()).toContain('暂无结论')
    expect(wrapper.text()).not.toContain('报告暂时不可用')
  })

  it('renders CSV evidence as one answer card without charts', async () => {
    adapter.getReport.mockResolvedValue({
      id: 'run-evidence',
      question: '15万以内的 SUV 有哪些样本',
      intent: 'evidence',
      scenarioLabel: '车源证据',
      generatedAt: '2026-09-18T00:00:00Z',
      dataAsOf: '项目清洗车源数据',
      summaryTitle: '代表性车源证据',
      summaryText: '已查询代表性车源样本。',
      answer: '## 查询结果\n\n已查询代表性车源样本。',
      reliability: { confidence: 'none', label: '暂无结论', sampleSize: 0, reason: '仅有查询命中数和代表性样本。' },
      filtersApplied: ['挂牌价在 -~15 万元'],
      methodology: '仅展示 csv_query 命中数和代表性样本。',
      limitations: ['代表性样本不等同于总体统计。'],
      evidence: {
        queryCount: 2,
        queries: [
          { filtersApplied: ['车系 = 元PLUS'], matchCount: 68, rows: [], columns: ['series'], note: '共命中 68 条。' },
          { filtersApplied: ['车系 = 哈弗H6'], matchCount: 19, rows: [], columns: ['series'], note: '共命中 19 条。' },
        ],
      },
    })
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [
        { path: '/', name: 'chat', component: { template: '<div />' } },
        { path: '/chat/:id', name: 'conversation', component: { template: '<div />' } },
        { path: '/chat', name: 'conversations', component: { template: '<div />' } },
        { path: '/analysis/:id', component: ReportPage },
      ],
    })
    await router.push('/analysis/run-evidence')
    await router.isReady()

    const wrapper = mount(ReportPage, { global: { plugins: [router] } })
    await flushPromises()

    expect(wrapper.text()).toContain('车源证据报告')
    expect(wrapper.text().match(/已查询代表性车源样本。/g)).toHaveLength(1)
    expect(wrapper.findAll('.chart-card')).toHaveLength(0)
    expect(wrapper.text()).toContain('68')
    expect(wrapper.text()).toContain('代表性样本不等同于总体统计')
  })

  it('renders an empty-data report with an honest unknown sample size', async () => {
    adapter.getReport.mockResolvedValue({
      id: 'run-empty',
      question: '查找不存在的车系',
      intent: 'no_data',
      scenarioLabel: '数据边界说明',
      generatedAt: '2026-09-18T00:00:00Z',
      dataAsOf: '项目清洗车源数据',
      summaryTitle: '暂未形成可比样本',
      summaryText: '当前条件没有命中可比车源。',
      answer: '建议先去掉城市限制后重试。',
      reliability: { confidence: 'none', label: '暂无结论', sampleSize: null, reason: '当前没有可比样本。' },
      filtersApplied: ['车系 = 不存在的车系'],
      methodology: '工具未形成有效样本，因此没有计算价格统计。',
      limitations: ['没有把无命中直接转换成可比样本数量。'],
      reasonCode: 'no_matching_samples',
      observed: { rawCount: 0 },
      relaxationCandidates: [{ field: 'city', action: 'remove', label: '去掉城市限制' }],
    })
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [
        { path: '/', name: 'chat', component: { template: '<div />' } },
        { path: '/chat/:id', name: 'conversation', component: { template: '<div />' } },
        { path: '/chat', name: 'conversations', component: { template: '<div />' } },
        { path: '/analysis/:id', component: ReportPage },
      ],
    })
    await router.push('/analysis/run-empty')
    await router.isReady()

    const wrapper = mount(ReportPage, { global: { plugins: [router] } })
    await flushPromises()

    expect(wrapper.text()).toContain('暂未形成可比样本')
    expect(wrapper.text()).toContain('未形成可比样本')
    expect(wrapper.text()).toContain('去掉城市限制')
    expect(wrapper.text()).not.toContain('0 条可比样本')
  })

  it('returns to the report conversation and preloads the original question for a deliberate reanalysis', async () => {
    adapter.getReport.mockResolvedValue({
      ...recommendationFixture,
      id: 'report-v2',
      question: '把预算改成 18 万，重新推荐',
      conversationId: 'conversation-7',
      reportVersion: 2,
    })
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [
        { path: '/', name: 'chat', component: { template: '<div />' } },
        { path: '/chat/:id', name: 'conversation', component: { template: '<div />' } },
        { path: '/chat', name: 'conversations', component: { template: '<div />' } },
        { path: '/analysis/:id', name: 'report', component: ReportPage },
      ],
    })
    await router.push('/analysis/report-v2')
    await router.isReady()
    const wrapper = mount(ReportPage, { global: { plugins: [router] } })
    await flushPromises()

    expect(wrapper.get('.back-link').attributes('href')).toBe('/chat/conversation-7')
    expect(wrapper.get('.next-action a').attributes('href')).toBe('/chat/conversation-7?draft=%E6%8A%8A%E9%A2%84%E7%AE%97%E6%94%B9%E6%88%90+18+%E4%B8%87%EF%BC%8C%E9%87%8D%E6%96%B0%E6%8E%A8%E8%8D%90&source_report_id=report-v2')
  })
})
