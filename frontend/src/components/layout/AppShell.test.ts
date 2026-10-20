import { mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import { describe, expect, it } from 'vitest'
import AppShell from './AppShell.vue'

describe('AppShell', () => {
  it.each([false, true])('renders the primary navigation and main landmark (chat workspace: %s)', async (chatWorkspace) => {
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [
        { path: '/', component: { template: '<div />' } },
        { path: '/dashboard', component: { template: '<div />' } },
        { path: '/chat', component: { template: '<div />' } },
        { path: '/chat/:id', component: { template: '<div />' } },
        { path: '/about', component: { template: '<div />' } },
      ],
    })
    await router.push('/')
    await router.isReady()

    const wrapper = mount(AppShell, {
      props: { chatWorkspace },
      global: { plugins: [router] },
      slots: { default: '<h1>内容</h1>' },
    })

    expect(wrapper.get('main').text()).toContain('内容')
    expect(wrapper.text()).toContain('智能分析')
    expect(wrapper.text()).toContain('市场看板')
    expect(wrapper.get('a[href="/chat"]').text()).toBe('聊天会话')
    expect(wrapper.text()).toContain('数据说明')
    expect(wrapper.findAll('nav[aria-label="主导航"]')).toHaveLength(1)
    expect(wrapper.findAll('nav[aria-label="主导航"] a')).toHaveLength(4)
    if (chatWorkspace) {
      await router.push('/chat/conversation-1')
      expect(wrapper.get('a[href="/chat"]').attributes('aria-current')).toBe('page')
      expect(wrapper.get('a[href="/chat"]').classes()).toContain('topbar__link--active')
    }
    wrapper.unmount()
  })
})
