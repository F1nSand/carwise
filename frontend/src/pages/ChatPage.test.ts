import { ref } from 'vue'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from '../App.vue'
import ChatPage from './ChatPage.vue'

const api = vi.hoisted(() => ({
  createConversation: vi.fn(), listConversations: vi.fn(), getConversation: vi.fn(),
  getConversationEntries: vi.fn(), sendMessage: vi.fn(), attachRun: vi.fn(),
}))
vi.mock('../services/conversationAdapter', () => ({ conversationAdapter: api }))
vi.mock('../services/analysisAdapter', () => ({ getDataCapabilities: vi.fn(async () => ({
  row_count: 143793, dataset_version: 'test-snapshot', fields: {}, energy_values: [],
  supported_filters: ['budget', 'city', 'brand', 'series', 'year', 'max_age'], unsupported_filters: [],
  data_time_range: { status: 'unknown', start: null, end: null },
})) }))
vi.mock('../composables/useAnalysisFlow', () => {
  const flow = {
    phase: ref('idle'), runtime: ref({ plan: [], activities: [], pendingInput: null, status: 'idle' }),
    runId: ref(''), report: ref(null), isSubmitting: ref(false), error: ref(''), failureCode: ref(''),
    requestId: ref(''), failureStage: ref(''), retryable: ref(false), attachRun: api.attachRun,
  }
  return { useAnalysisFlow: () => flow, retainAnalysisFlow: () => ({ flow, release: vi.fn() }) }
})

const metadata = {
  conversationId: 'conversation-first', contextId: 'context-first', title: '新会话',
  titleSource: 'untitled', createdAt: '2026-10-06', updatedAt: '2026-10-06',
  conversationRevision: 0, hasPendingInput: false,
}
let wrapper: VueWrapper
const animate = vi.fn(() => ({ finished: Promise.resolve(), cancel: vi.fn() }))

async function render(path = '/') {
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: '/', name: 'chat', component: ChatPage },
    { path: '/chat', name: 'conversations', component: ChatPage },
    { path: '/chat/:id', name: 'conversation', component: ChatPage },
    { path: '/dashboard', component: { template: '<div />' } },
    { path: '/history', redirect: '/chat' },
    { path: '/about', component: { template: '<div />' } },
  ] })
  await router.push(path)
  await router.isReady()
  wrapper = mount(App, { attachTo: document.body, global: {
    plugins: [router], stubs: { MessageBubble: { props: ['content'], template: '<div class="message-bubble">{{ content }}</div>' } },
  } })
  await flushPromises()
  return router
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.stubGlobal('matchMedia', vi.fn(() => ({ matches: false })))
  vi.spyOn(window, 'scrollTo').mockImplementation(() => {})
  Object.defineProperty(HTMLElement.prototype, 'scrollTo', { configurable: true, value: vi.fn() })
  Object.defineProperty(HTMLElement.prototype, 'animate', { configurable: true, value: animate })
  api.createConversation.mockResolvedValue({ conversationId: metadata.conversationId, contextId: metadata.contextId, metadata })
  api.listConversations.mockResolvedValue({ items: [], nextCursor: null })
  api.attachRun.mockResolvedValue(undefined)
  api.getConversation.mockResolvedValue(metadata)
  api.getConversationEntries.mockResolvedValue({ conversationId: metadata.conversationId, entries: [], highWaterSeq: 0 })
  api.sendMessage.mockImplementation(async (id, content, commandId) => ({
    acceptedKind: 'run', runId: 'run-first', entry: { entryId: 'user-first', conversationId: id,
      conversationSeq: 1, kind: 'user_message', sourceKey: `message:${commandId}`,
      payload: { content, delivery_status: 'sent' }, createdAt: '2026-10-06' },
  }))
})
afterEach(() => { wrapper?.unmount(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

describe('homepage to conversation', () => {
  it('keeps the landing page when focusing, typing and choosing a scenario or example', async () => {
    const router = await render()
    expect(wrapper.text()).toContain('看清每一辆车，')
    expect(wrapper.text()).toContain('智能分析')
    expect(wrapper.find('.conversation-sidebar').exists()).toBe(false)
    await wrapper.get('#query-input').trigger('focus')
    await wrapper.get('#query-input').setValue('15 万预算')
    await wrapper.get('.example').trigger('click')
    await wrapper.get('.capability').trigger('click')
    expect(router.currentRoute.value.path).toBe('/')
    expect(wrapper.find('.landing').exists()).toBe(true)
    expect(api.createConversation).not.toHaveBeenCalled()
    expect(api.sendMessage).not.toHaveBeenCalled()
  })

  it('switches once on submit, animates the composer and keeps the first message', async () => {
    const router = await render()
    const originalPageId = wrapper.getComponent(ChatPage).vm.$.uid
    await wrapper.get('#query-input').setValue('比较 A4L 和宝马 3 系')
    await wrapper.get('form.composer').trigger('submit')
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/chat/conversation-first')
    expect(wrapper.getComponent(ChatPage).vm.$.uid).toBe(originalPageId)
    expect(wrapper.find('.landing').exists()).toBe(false)
    expect(wrapper.find('.topbar').exists()).toBe(true)
    expect(wrapper.find('.conversation-sidebar').exists()).toBe(true)
    expect(wrapper.get('.message-bubble').text()).toBe('比较 A4L 和宝马 3 系')
    expect(api.createConversation).toHaveBeenCalledTimes(1)
    expect(api.sendMessage).toHaveBeenCalledWith('conversation-first', '比较 A4L 和宝马 3 系', expect.any(String), null)
    expect(api.attachRun).toHaveBeenCalledWith('run-first')
    expect(animate).toHaveBeenCalledTimes(1)
    expect(document.activeElement?.id).toBe('chat-message')
  })

  it('keeps the homepage and draft if creating a conversation fails', async () => {
    api.createConversation.mockRejectedValueOnce(new Error('暂时无法创建会话'))
    const router = await render()
    await wrapper.get('#query-input').setValue('A4L 行情')
    await wrapper.get('form.composer').trigger('submit')
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/')
    expect((wrapper.get('#query-input').element as HTMLTextAreaElement).value).toBe('A4L 行情')
    expect(wrapper.get('[role="alert"]').text()).toContain('暂时无法创建会话')
    expect(api.sendMessage).not.toHaveBeenCalled()
  })

  it('retains the failed message and restores the draft after entering chat', async () => {
    api.sendMessage.mockRejectedValueOnce(new Error('消息暂时无法发送'))
    const router = await render()
    await wrapper.get('#query-input').setValue('A4L 行情')
    await wrapper.get('form.composer').trigger('submit')
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/chat/conversation-first')
    expect(wrapper.get('.message-bubble').text()).toBe('A4L 行情')
    expect((wrapper.get('#chat-message').element as HTMLTextAreaElement).value).toBe('A4L 行情')
    expect(wrapper.get('.chat-send-error').text()).toContain('消息暂时无法发送')
    expect(api.attachRun).not.toHaveBeenCalled()
  })

  it('keeps a failed conversation A send out of conversation B draft and error state', async () => {
    let rejectSend!: (error: Error) => void
    api.sendMessage.mockImplementationOnce(() => new Promise((_resolve, reject) => { rejectSend = reject }))
    const router = await render('/chat/conversation-first')
    await wrapper.get('#chat-message').setValue('A 的请求')
    await wrapper.get('form.chat-composer').trigger('submit')
    await vi.waitFor(() => expect(api.sendMessage).toHaveBeenCalled())

    const secondMetadata = { ...metadata, conversationId: 'conversation-second', contextId: 'context-second', title: '第二个会话' }
    api.getConversation.mockImplementation(async (id: string) => id === 'conversation-second' ? secondMetadata : metadata)
    api.getConversationEntries.mockImplementation(async (id: string) => ({ conversationId: id, entries: [], highWaterSeq: 0 }))
    await router.push('/chat/conversation-second')
    await flushPromises()
    await wrapper.get('#chat-message').setValue('B 的草稿')
    rejectSend(new Error('A 发送失败'))
    await flushPromises()

    expect((wrapper.get('#chat-message').element as HTMLTextAreaElement).value).toBe('B 的草稿')
    expect(wrapper.find('.chat-send-error').exists()).toBe(false)
    expect(router.currentRoute.value.path).toBe('/chat/conversation-second')
  })

  it('does not let a successful delayed A response navigate away from conversation B', async () => {
    let acceptSend!: (value: Awaited<ReturnType<typeof api.sendMessage>>) => void
    api.sendMessage.mockImplementationOnce(() => new Promise((resolve) => { acceptSend = resolve }))
    const router = await render('/chat/conversation-first')
    await wrapper.get('#chat-message').setValue('A 的请求')
    await wrapper.get('form.chat-composer').trigger('submit')
    await vi.waitFor(() => expect(api.sendMessage).toHaveBeenCalled())

    const secondMetadata = { ...metadata, conversationId: 'conversation-second', contextId: 'context-second', title: '第二个会话' }
    api.getConversation.mockImplementation(async (id: string) => id === 'conversation-second' ? secondMetadata : metadata)
    api.getConversationEntries.mockImplementation(async (id: string) => ({ conversationId: id, entries: [], highWaterSeq: 0 }))
    await router.push({ name: 'conversation', params: { id: 'conversation-second' }, query: { draft: 'B 的链接草稿' } })
    await flushPromises()
    acceptSend({ acceptedKind: 'run', runId: 'run-first', entry: { entryId: 'user-first', conversationId: 'conversation-first', conversationSeq: 1, kind: 'user_message', sourceKey: 'message:old', payload: { content: 'A 的请求' }, createdAt: '2026-10-06' } })
    await flushPromises()

    expect(router.currentRoute.value.path).toBe('/chat/conversation-second')
    expect((wrapper.get('#chat-message').element as HTMLTextAreaElement).value).toBe('B 的链接草稿')
  })

  it('does not activate or navigate to a newly created A after the user opens B', async () => {
    let finishCreation!: (value: typeof metadata) => void
    api.createConversation.mockReturnValueOnce(new Promise((resolve) => { finishCreation = resolve }).then((created: any) => ({ conversationId: created.conversationId, contextId: created.contextId, metadata: created })))
    const router = await render('/')
    await wrapper.get('#query-input').setValue('A 的请求')
    await wrapper.get('form.composer').trigger('submit')
    await vi.waitFor(() => expect(api.createConversation).toHaveBeenCalled())

    const secondMetadata = { ...metadata, conversationId: 'conversation-second', contextId: 'context-second', title: '第二个会话' }
    api.getConversation.mockImplementation(async (id: string) => id === 'conversation-second' ? secondMetadata : metadata)
    api.getConversationEntries.mockImplementation(async (id: string) => ({ conversationId: id, entries: [], highWaterSeq: 0 }))
    await router.push('/chat/conversation-second')
    await flushPromises()
    finishCreation(metadata)
    await flushPromises()

    expect(router.currentRoute.value.path).toBe('/chat/conversation-second')
    expect(api.sendMessage).toHaveBeenCalledWith('conversation-first', 'A 的请求', expect.any(String), null)
  })

  it('loads the next 50-conversation page in the sidebar', async () => {
    const firstPage = Array.from({ length: 50 }, (_, index) => ({ ...metadata, conversationId: `conversation-${index}`, title: `会话 ${index}` }))
    api.listConversations.mockResolvedValueOnce({ items: firstPage, nextCursor: 'cursor-2' })
      .mockResolvedValueOnce({ items: [{ ...metadata, conversationId: 'conversation-50', title: '会话 50' }], nextCursor: null })
    await render('/chat')
    expect(wrapper.text()).toContain('会话 0')
    await wrapper.get('.conversation-sidebar__more').trigger('click')
    await flushPromises()
    expect(api.listConversations).toHaveBeenLastCalledWith('cursor-2', 50)
    expect(wrapper.text()).toContain('会话 50')
    expect(wrapper.find('.conversation-sidebar__more').exists()).toBe(false)
  })

  it('loads earlier entries from the cursor while keeping them merged with the newest page', async () => {
    const entries = (start: number, end: number, id: string) => Array.from({ length: end - start + 1 }, (_, index) => {
      const seq = start + index
      return { entryId: `entry-${seq}`, conversationId: id, conversationSeq: seq, kind: 'user_message', sourceKey: `source-${seq}`, payload: { text: `消息 ${seq}` }, createdAt: '2026-10-06' }
    })
    api.getConversationEntries.mockImplementation(async (id: string, beforeSeq?: number | null) => beforeSeq === 51
      ? { conversationId: id, entries: entries(1, 1, id), highWaterSeq: 51, nextBeforeSeq: null }
      : { conversationId: id, entries: entries(2, 51, id), highWaterSeq: 51, nextBeforeSeq: 51 })
    await render('/chat/conversation-first')
    expect(wrapper.findAll('.message-bubble')).toHaveLength(50)
    const element = wrapper.get('.chat-thread').element as HTMLElement
    let scrollHeight = 300
    let scrollTop = 200
    Object.defineProperty(element, 'scrollHeight', { configurable: true, get: () => scrollHeight })
    Object.defineProperty(element, 'scrollTop', { configurable: true, get: () => scrollTop, set: (value: number) => { scrollTop = value } })
    api.getConversationEntries.mockImplementationOnce(async (id: string) => {
      scrollHeight = 400
      return { conversationId: id, entries: entries(1, 1, id), highWaterSeq: 51, nextBeforeSeq: null }
    })
    await wrapper.get('.load-older').trigger('click')
    await flushPromises()
    expect(api.getConversationEntries).toHaveBeenLastCalledWith('conversation-first', 51, 50, undefined)
    expect(wrapper.findAll('.message-bubble')).toHaveLength(51)
    expect(scrollTop).toBe(300)
    expect(wrapper.find('.load-older').exists()).toBe(false)
  })

  it('switches without motion when reduced motion is requested', async () => {
    vi.stubGlobal('matchMedia', vi.fn(() => ({ matches: true })))
    await render()
    await wrapper.get('#query-input').setValue('A4L 行情')
    await wrapper.get('form.composer').trigger('submit')
    await flushPromises()
    expect(wrapper.find('.chat-workspace').exists()).toBe(true)
    expect(wrapper.find('.chat-workspace--entering').exists()).toBe(false)
    expect(animate).not.toHaveBeenCalled()
  })

  it('unlocks the chat input after acceptance while the Agent event stream is still running', async () => {
    let completeRun!: () => void
    api.attachRun.mockReturnValueOnce(new Promise<void>((resolve) => { completeRun = resolve }))
    await render()
    await wrapper.get('#query-input').setValue('A4L 行情')
    await wrapper.get('form.composer').trigger('submit')
    await flushPromises()
    expect((wrapper.get('#chat-message').element as HTMLTextAreaElement).disabled).toBe(false)
    await wrapper.get('#chat-message').setValue('再比较上海的挂牌价')
    expect((wrapper.get('#chat-message').element as HTMLTextAreaElement).value).toBe('再比较上海的挂牌价')
    completeRun()
    await flushPromises()
  })

  it('opens an existing conversation and keeps the sidebar when starting a new one', async () => {
    const router = await render('/chat/conversation-first')
    expect(wrapper.find('.home-page').exists()).toBe(false)
    expect(wrapper.find('.conversation-sidebar').exists()).toBe(true)
    await wrapper.get('.new-conversation').trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/chat')
    expect(wrapper.find('.home-page').exists()).toBe(false)
    expect(wrapper.find('.conversation-sidebar').exists()).toBe(true)
    expect(api.createConversation).not.toHaveBeenCalled()
  })

  it('opens the conversation workspace from the navigation and sends a first message', async () => {
    const router = await render()
    await wrapper.get('a[href="/chat"]').trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/chat')
    expect(wrapper.find('.conversation-sidebar').exists()).toBe(true)
    expect(api.createConversation).not.toHaveBeenCalled()
    await wrapper.get('#chat-message').setValue('A4L 行情')
    await wrapper.get('form.chat-composer').trigger('submit')
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/chat/conversation-first')
    expect(api.sendMessage).toHaveBeenCalled()
  })

  it('redirects the retired history URL to the conversation workspace', async () => {
    const router = await render('/history')
    expect(router.currentRoute.value.path).toBe('/chat')
    expect(wrapper.find('.conversation-sidebar').exists()).toBe(true)
  })
})
