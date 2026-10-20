import { ref } from 'vue'
import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import HomePage from './HomePage.vue'

const flow = {
  text: ref('问题'),
  phase: ref<'idle' | 'running' | 'awaiting_input' | 'error'>('awaiting_input'),
  draft: ref(null),
  error: ref(''),
  failureCode: ref(''),
  requestId: ref(''),
  failureStage: ref(''),
  retryable: ref(false),
  isSubmitting: ref(false),
  transportState: ref<'live' | 'reconnecting' | 'offline'>('offline'),
  correctionStatus: ref<'idle' | 'submitting' | 'received' | 'applied' | 'error'>('idle'),
  correctionError: ref(''),
  runtime: ref({
    plan: [{ id: 'query', title: '查询', description: '查询数据', toolNames: ['recommend'], status: 'complete' as const }],
    activities: [],
    pendingInput: { questions: [] },
    waitState: { status: 'waiting_user', toolCallId: 'call-ask' },
    status: 'waiting_user',
    capabilities: { midrunInput: false, recover: false },
  }),
  reportId: ref(''),
  runId: ref('run-test'),
  submit: vi.fn(),
  submitInput: vi.fn(async () => { flow.reportId.value = 'run-resumed'; flow.runtime.value.status = 'done' }),
  confirm: vi.fn(),
  attachRun: vi.fn(async () => undefined),
  submitCorrection: vi.fn(),
  retry: vi.fn(),
  reset: vi.fn(),
}

vi.mock('../composables/useAnalysisFlow', () => ({
  useAnalysisFlow: () => flow,
}))

describe('HomePage', () => {
  beforeEach(() => {
    flow.reportId.value = ''
    flow.submitInput.mockClear()
    flow.submit.mockClear()
    flow.attachRun.mockClear()
    flow.error.value = ''
    flow.failureCode.value = ''
    flow.requestId.value = ''
    flow.failureStage.value = ''
    flow.retryable.value = false
    flow.phase.value = 'awaiting_input'
    flow.runtime.value.capabilities = { midrunInput: false, recover: false }
  })

  it('navigates to the report after a waiting Agent run resumes', async () => {
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [
        { path: '/', component: HomePage },
        { path: '/analysis/:id', component: { template: '<div />' } },
      ],
    })
    await router.push('/')
    await router.isReady()
    const wrapper = mount(HomePage, {
      global: {
        plugins: [router],
        stubs: {
          QueryComposer: true,
          ScenarioCards: true,
          AgentRuntimePanel: true,
          ParameterReview: true,
          AgentQuestionCard: {
            template: '<button @click="$emit(\'submit\', [])">继续</button>',
          },
        },
      },
    })

    await wrapper.get('button').trigger('click')
    await flushPromises()

    expect(flow.submitInput).toHaveBeenCalledWith([])
    expect(router.currentRoute.value.fullPath).toBe('/analysis/run-resumed')
  })

  it('navigates once when a new analysis finishes from the adapter terminal snapshot', async () => {
    flow.phase.value = 'idle'
    flow.submit.mockImplementation(async () => { flow.reportId.value = 'run-snapshot'; flow.runtime.value.status = 'done' })
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [
        { path: '/', component: HomePage },
        { path: '/analysis/:id', component: { template: '<div />' } },
      ],
    })
    await router.push('/')
    await router.isReady()
    const wrapper = mount(HomePage, {
      global: {
        plugins: [router],
        stubs: {
          QueryComposer: { template: '<button @click="$emit(\'submit\', \'问题\')">发送</button>' },
          ScenarioCards: true,
        },
      },
    })

    await wrapper.get('button').trigger('click')
    await flushPromises()

    expect(flow.submit).toHaveBeenCalledWith('问题')
    expect(router.currentRoute.value.fullPath).toBe('/analysis/run-snapshot')
  })

  it('attaches a recovered or queued run from the resume route query', async () => {
    flow.phase.value = 'idle'
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [
        { path: '/', component: HomePage },
        { path: '/analysis/:id', component: { template: '<div />' } },
      ],
    })
    await router.push('/?resume_run=run-recovered')
    await router.isReady()
    mount(HomePage, {
      global: {
        plugins: [router],
        stubs: { QueryComposer: true, ScenarioCards: true },
      },
    })
    await flushPromises()

    expect(flow.attachRun).toHaveBeenCalledWith('run-recovered')
    expect(router.currentRoute.value.fullPath).toBe('/?resume_run=run-recovered')
  })

  it('shows mid-run correction only when the server advertises the capability', async () => {
    flow.phase.value = 'running'
    flow.runtime.value.pendingInput = null
    flow.runtime.value.capabilities = { midrunInput: true, recover: false }
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/', component: HomePage }] })
    await router.push('/')
    await router.isReady()
    const wrapper = mount(HomePage, {
      global: {
        plugins: [router],
        stubs: { QueryComposer: true, ScenarioCards: true, AgentRuntimePanel: true },
      },
    })

    expect(wrapper.text()).toContain('补充或修改条件')
  })

  it('keeps transport reconnection separate from the running Agent status', async () => {
    flow.phase.value = 'running'
    flow.transportState.value = 'reconnecting'
    flow.runtime.value.status = 'running'
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/', component: HomePage }] })
    await router.push('/')
    await router.isReady()
    const wrapper = mount(HomePage, {
      global: {
        plugins: [router],
        stubs: { QueryComposer: true, ScenarioCards: true, AgentRuntimePanel: true },
      },
    })

    expect(wrapper.text()).toContain('正在重新连接；分析仍在后台运行。')
    expect(flow.runtime.value.status).toBe('running')
  })

  it('keeps terminal run failures with the Agent workspace instead of the query field', async () => {
    flow.phase.value = 'error'
    flow.error.value = '数据已查询，但最终答案未通过校验。'
    flow.failureCode.value = 'answer_invalid'
    flow.requestId.value = 'req-test-123'
    flow.failureStage.value = 'answer_validation'
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/', component: HomePage }] })
    await router.push('/')
    await router.isReady()
    const wrapper = mount(HomePage, {
      global: {
        plugins: [router],
        stubs: {
          QueryComposer: { props: ['error', 'errorCode'], template: '<div data-testid="query-error">{{ error }}</div>' },
          AgentRuntimePanel: { template: '<div data-testid="runtime-panel" />' },
          ScenarioCards: true,
        },
      },
    })

    expect(wrapper.get('[data-testid="query-error"]').text()).toBe('')
    expect(wrapper.get('[data-testid="run-error"]').text()).toContain('数据已查询，但最终答案未通过校验。')
    expect(wrapper.get('[data-testid="run-error"]').text()).toContain('answer_validation')
    expect(wrapper.get('[data-testid="run-error"]').text()).toContain('req-test-123')
    expect(wrapper.find('[data-testid="runtime-panel"]').exists()).toBe(true)
  })

  it('shows request diagnostics for submission failures before a run is created', async () => {
    flow.phase.value = 'error'
    flow.runId.value = ''
    flow.error.value = '请求处理失败。'
    flow.failureCode.value = 'storage_busy'
    flow.requestId.value = 'req-submit-456'
    flow.failureStage.value = 'persistence'
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/', component: HomePage }] })
    await router.push('/')
    await router.isReady()
    const wrapper = mount(HomePage, {
      global: {
        plugins: [router],
        stubs: {
          QueryComposer: { props: ['error', 'errorCode', 'errorRequestId', 'errorStage'], template: '<div data-testid="query-error">{{ error }} {{ errorCode }} {{ errorStage }} {{ errorRequestId }}</div>' },
          ScenarioCards: true,
        },
      },
    })

    expect(wrapper.get('[data-testid="query-error"]').text()).toContain('persistence')
    expect(wrapper.get('[data-testid="query-error"]').text()).toContain('req-submit-456')
  })
})
