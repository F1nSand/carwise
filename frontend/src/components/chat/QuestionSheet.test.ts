import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import { afterEach, describe, expect, it } from 'vitest'
import QuestionSheet from './QuestionSheet.vue'
import type { PendingInput } from '../../types/analysis'

const pendingInput: PendingInput = {
  tool_call_id: 'call-ask',
  step: 2,
  questions: [{
    id: 'budget', prompt: '预算上限是多少？',
    options: [{ id: 'a', label: '15 万以内', value: '15' }],
    allow_custom: true, required: true, selection_mode: 'single', max_selections: 1,
  }],
}

afterEach(() => {
  document.body.innerHTML = ''
  document.body.style.overflow = ''
})

describe('QuestionSheet', () => {
  it('allows closing the sheet while an answer request is pending', async () => {
    const wrapper = mount(QuestionSheet, {
      attachTo: document.body,
      props: { open: true, pendingInput, identity: 'chat:run:ask:1', draft: null, busy: true },
    })
    await nextTick()
    document.querySelector<HTMLButtonElement>('.question-sheet__close')?.click()
    await nextTick()
    expect(wrapper.emitted('update:open')).toEqual([[false]])
    wrapper.unmount()
  })
  it('focuses a selectable answer, locks background scroll, and restores focus on close', async () => {
    const trigger = document.createElement('button')
    trigger.textContent = '回答问题'
    document.body.append(trigger)
    trigger.focus()
    const wrapper = mount(QuestionSheet, {
      attachTo: document.body,
      props: { open: true, pendingInput, identity: 'chat:run:ask:1', draft: null },
    })

    await nextTick()
    await nextTick()
    expect(document.activeElement?.classList.contains('option-button')).toBe(true)
    expect(document.body.style.overflow).toBe('hidden')

    await wrapper.setProps({ open: false })
    await nextTick()
    expect(document.activeElement).toBe(trigger)
    expect(document.body.style.overflow).toBe('')
    wrapper.unmount()
  })

  it('does not focus a custom text field when the panel opens', async () => {
    const customOnly: PendingInput = {
      ...pendingInput,
      tool_call_id: 'call-custom',
      questions: [{ ...pendingInput.questions[0], options: [] }],
    }
    const wrapper = mount(QuestionSheet, {
      attachTo: document.body,
      props: { open: true, pendingInput: customOnly, identity: 'chat:run:ask:2', draft: null },
    })
    await nextTick()
    await nextTick()

    expect((document.activeElement as HTMLElement).tagName).toBe('BUTTON')
    expect(document.activeElement).not.toBe(document.querySelector('.question-sheet input'))
    wrapper.unmount()
  })
})
