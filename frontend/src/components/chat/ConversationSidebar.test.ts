import { defineComponent, nextTick, ref } from 'vue'
import { mount } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'
import ConversationSidebar from './ConversationSidebar.vue'

const emptyProps = { items: [], currentConversationId: null }
afterEach(() => { vi.unstubAllGlobals() })

describe('ConversationSidebar mobile keyboard behavior', () => {
  it('makes the closed drawer inert and restores focus to its trigger after Escape', async () => {
    vi.stubGlobal('matchMedia', vi.fn(() => ({ matches: true, addEventListener: vi.fn(), removeEventListener: vi.fn() })))
    const Harness = defineComponent({
      components: { ConversationSidebar },
      setup() { const open = ref(false); return { open, emptyProps } },
      template: '<button class="menu" @click="open = true">Open</button><ConversationSidebar v-bind="emptyProps" :open="open" @close="open = false" />',
    })
    const wrapper = mount(Harness, { attachTo: document.body, global: { stubs: { RouterLink: { props: ['to'], template: '<a :href="to"><slot /></a>' } } } })
    await nextTick()
    expect(wrapper.get('.conversation-sidebar').attributes('inert')).toBeDefined()

    wrapper.get('.menu').element.focus()
    await wrapper.get('.menu').trigger('click')
    await nextTick()
    expect(document.activeElement).toBe(wrapper.get('.conversation-sidebar__close').element)
    await wrapper.get('.conversation-sidebar').trigger('keydown', { key: 'Escape' })
    await nextTick()
    expect(wrapper.get('.conversation-sidebar').attributes('inert')).toBeDefined()
    expect(document.activeElement).toBe(wrapper.get('.menu').element)
    wrapper.unmount()
  })
})
