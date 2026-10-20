import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import AgentQuestionCard from './AgentQuestionCard.vue'
import type { PendingInput } from '../../types/analysis'

const pendingInput: PendingInput = {
  tool_call_id: 'call-ask',
  step: 2,
  questions: [
    {
      id: 'budget',
      prompt: '预算上限是多少？',
      options: [{ id: 'a', label: '15 万以内', value: '15' }],
      allow_custom: true,
      required: true,
      selection_mode: 'single',
      max_selections: 1,
    },
    {
      id: 'city',
      prompt: '优先哪个城市？',
      options: [],
      allow_custom: true,
      required: true,
      selection_mode: 'single',
      max_selections: 1,
    },
  ],
}

describe('AgentQuestionCard', () => {
  it('answers questions sequentially and allows going back to edit', async () => {
    const wrapper = mount(AgentQuestionCard, { props: { pendingInput } })

    await wrapper.get('button.option-button').trigger('click')
    await wrapper.get('button[aria-label="下一题"]').trigger('click')
    await wrapper.get('input[type="text"]').setValue('上海')
    await wrapper.get('button[aria-label="上一题"]').trigger('click')
    expect(wrapper.text()).toContain('预算上限是多少？')
    await wrapper.get('button.option-button').trigger('click')
    await wrapper.get('button[aria-label="下一题"]').trigger('click')
    await wrapper.get('button[aria-label="提交回答"]').trigger('click')

    expect(wrapper.emitted('submit')?.[0][0]).toEqual([
      { question_id: 'budget', values: ['15'] },
      { question_id: 'city', values: ['上海'] },
    ])
  })

  it('blocks the next question until a required answer is present', async () => {
    const wrapper = mount(AgentQuestionCard, { props: { pendingInput } })

    await wrapper.get('button[aria-label="下一题"]').trigger('click')

    expect(wrapper.text()).toContain('请先回答这个问题')
    expect(wrapper.text()).toContain('预算上限是多少？')
  })

  it('uses stable A/B/C labels instead of model option ids and locks while submitting', () => {
    const wrapper = mount(AgentQuestionCard, {
      props: {
        pendingInput: {
          ...pendingInput,
          questions: [{
            ...pendingInput.questions[0],
            options: [
              { id: 'budget-low', label: '10 万以内', value: '10' },
              { id: 'budget-mid', label: '15 万以内', value: '15' },
              { id: 'budget-high', label: '20 万以内', value: '20' },
            ],
          }],
        },
        busy: true,
      },
    })

    expect(wrapper.findAll('.option-button__mark').map((item) => item.text())).toEqual(['A', 'B', 'C'])
    expect(wrapper.find('.custom-answer').text()).toContain('D')
    expect(wrapper.get('input').attributes('placeholder')).toBe('请输入自己的答案')
    expect(wrapper.findAll('button.option-button').every((button) => (button.element as HTMLButtonElement).disabled)).toBe(true)
    expect((wrapper.get('input').element as HTMLInputElement).disabled).toBe(true)
    expect((wrapper.get('button[aria-label="提交回答"]').element as HTMLButtonElement).disabled).toBe(true)
  })

  it('keeps the custom input empty after selecting an option', async () => {
    const wrapper = mount(AgentQuestionCard, { props: { pendingInput } })

    await wrapper.get('button.option-button').trigger('click')

    expect((wrapper.get('input[type="text"]').element as HTMLInputElement).value).toBe('')
  })

  it('numbers the custom answer after the current options', () => {
    const wrapper = mount(AgentQuestionCard, {
      props: {
        pendingInput: {
          ...pendingInput,
          questions: [{
            ...pendingInput.questions[0],
            options: [
              { id: 'a', label: '10 万以内', value: '10' },
              { id: 'b', label: '15 万以内', value: '15' },
            ],
          }],
        },
      },
    })

    expect(wrapper.findAll('.answer-row__mark').map((item) => item.text())).toEqual(['A', 'B', 'C'])
    expect(wrapper.find('.options').element.contains(wrapper.find('.custom-answer').element)).toBe(true)
  })

  it('uses custom text only after the user types it', async () => {
    const wrapper = mount(AgentQuestionCard, {
      props: {
        pendingInput: {
          ...pendingInput,
          questions: [pendingInput.questions[0]],
        },
      },
    })

    await wrapper.get('button.option-button').trigger('click')
    await wrapper.get('input[type="text"]').setValue('17 万以内')
    await wrapper.get('button[aria-label="提交回答"]').trigger('click')

    expect(wrapper.emitted('submit')?.[0][0]).toEqual([{ question_id: 'budget', values: ['17 万以内'] }])
  })

  it('supports multiple selections, toggling and custom values together', async () => {
    const wrapper = mount(AgentQuestionCard, {
      props: {
        pendingInput: {
          ...pendingInput,
          questions: [{
            ...pendingInput.questions[0],
            selection_mode: 'multiple',
            max_selections: 3,
            options: [
              { id: 'a', label: '哈弗 H6', value: '哈弗 H6' },
              { id: 'b', label: '长安 CS75', value: '长安 CS75' },
              { id: 'c', label: '比亚迪宋', value: '比亚迪宋' },
            ],
          }],
        },
      },
    })

    const options = wrapper.findAll('button.option-button')
    await options[0].trigger('click')
    await options[2].trigger('click')
    await options[0].trigger('click')
    await options[0].trigger('click')
    await wrapper.get('input[type="text"]').setValue('吉利博越')
    await wrapper.get('button[aria-label="提交回答"]').trigger('click')

    expect(wrapper.emitted('submit')?.[0][0]).toEqual([
      { question_id: 'budget', values: ['比亚迪宋', '哈弗 H6', '吉利博越'] },
    ])
  })
})
