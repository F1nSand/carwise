import { mount, flushPromises, type VueWrapper } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import { afterEach, expect, it, vi } from 'vitest'
import ChatPage from './ChatPage.vue'
import QuestionSheet from '../components/chat/QuestionSheet.vue'

// Keep the real flow, adapter hydration, page watcher and question component.
// Only HTTP/stream boundaries are faked to reproduce the worker race exactly.
const transport = vi.hoisted(() => ({ getRun: vi.fn(), submitRunInput: vi.fn(), resumeAnalysis: vi.fn() }))
vi.mock('../services/analysisAdapter', async (original) => ({
  ...await original<typeof import('../services/analysisAdapter')>(),
  ...transport, getDataCapabilities: vi.fn(async () => null),
}))
const metadata = { conversationId: 'question-recovery', contextId: 'question-recovery', title: '选车',
  activeRunId: 'recovery-run', activeRunStatus: 'waiting_user', createdAt: '', updatedAt: '', conversationRevision: 0 }
vi.mock('../services/conversationAdapter', () => ({ conversationAdapter: {
  getConversation: vi.fn(async () => metadata),
  getConversationEntries: vi.fn(async () => ({ conversationId: metadata.conversationId, entries: [], highWaterSeq: 0 })),
  listConversations: vi.fn(async () => ({ items: [], nextCursor: null })),
} }))

let wrapper: VueWrapper | undefined
afterEach(() => { wrapper?.unmount(); document.body.innerHTML = ''; vi.restoreAllMocks() })

it('closes the submitted sheet across resumed snapshots and only opens a genuinely new question', async () => {
  vi.stubGlobal('matchMedia', vi.fn(() => ({ matches: true })))
  Object.defineProperty(HTMLElement.prototype, 'scrollTo', { configurable: true, value: vi.fn() })
  const pending = { tool_call_id: 'ask-first', step: 1, resume_revision: 1,
    questions: [{ id: 'budget', prompt: '预算是多少？', options: [{ id: 'a', label: '15万以内', value: '15万以内' }], required: true }] }
  transport.getRun.mockResolvedValue({ run_id: 'recovery-run', context_id: 'question-recovery', input: '选车',
    status: 'waiting_user', attempt: 1, resume_revision: 0, last_event_seq: 10, pending_input: pending })
  transport.submitRunInput.mockResolvedValue({ accepted_tool_call_id: 'ask-first', resume_revision: 1 })
  let release!: () => void
  const gate = new Promise<void>((resolve) => { release = resolve })
  transport.resumeAnalysis.mockImplementation(async function* (_id, options) {
    options.onSnapshot?.({ run_id: 'recovery-run', status: 'running', attempt: 2, resume_revision: 1,
      pending_input: pending, runtime: { pending_input: pending } })
    yield { kind: 'input_required', seq: 11, attempt: 2,
      pendingInput: { tool_call_id: 'ask-first', step: 1, resumeRevision: 1, questions: pending.questions } }
    await gate
    yield { kind: 'input_required', seq: 12, attempt: 2,
      pendingInput: { tool_call_id: 'ask-next', step: 2, resumeRevision: 1,
        questions: [{ id: 'city', prompt: '在哪个城市？', options: [], allow_custom: true, required: true }] } }
    return null
  })
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: '/chat/:id', name: 'conversation', component: ChatPage },
    { path: '/dashboard', component: { template: '<div />' } },
    { path: '/about', component: { template: '<div />' } },
  ] })
  await router.push('/chat/question-recovery')
  await router.isReady()
  wrapper = mount(ChatPage, { attachTo: document.body, global: { plugins: [router] } })
  await flushPromises()
  expect(document.querySelector('[role="dialog"]')).not.toBeNull()
  document.querySelector<HTMLButtonElement>('.option-button')!.click()
  await flushPromises()
  document.querySelector<HTMLButtonElement>('[aria-label="提交回答"]')!.click()
  await flushPromises()
  expect(transport.submitRunInput).toHaveBeenCalledTimes(1)
  expect(transport.submitRunInput).toHaveBeenCalledWith('recovery-run',
    [{ question_id: 'budget', values: ['15万以内'] }], { toolCallId: 'ask-first', resumeRevision: 0 },
    expect.any(AbortSignal), expect.any(String))
  expect(wrapper.findComponent(QuestionSheet).exists()).toBe(false)
  expect(document.querySelector('[role="dialog"]')).toBeNull()
  expect(wrapper.find('.question-prompt').exists()).toBe(false)
  release()
  await flushPromises()
  expect(document.querySelector('[role="dialog"]')?.textContent).toContain('在哪个城市？')
  expect(transport.submitRunInput).toHaveBeenCalledTimes(1)
})
