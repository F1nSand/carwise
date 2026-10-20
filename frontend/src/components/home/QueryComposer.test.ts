import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import QueryComposer from './QueryComposer.vue'

describe('QueryComposer', () => {
  it('preserves long prompts for the backend length and token checks', async () => {
    const prompt = '多阶段分析任务'.repeat(40)
    const wrapper = mount(QueryComposer, { props: { modelValue: prompt } })
    const textarea = wrapper.get('textarea')

    expect(textarea.attributes('maxlength')).toBeUndefined()
    expect((textarea.element as HTMLTextAreaElement).value).toBe(prompt)

    const nextPrompt = `${prompt}请保留最后一段`
    await textarea.setValue(nextPrompt)

    expect(wrapper.emitted('update:modelValue')?.at(-1)).toEqual([nextPrompt])
  })

  it('renders safe stage and request ID diagnostics with submission errors', () => {
    const wrapper = mount(QueryComposer, {
      props: {
        modelValue: '问题',
        error: '请求处理失败。',
        errorCode: 'internal_error',
        errorStage: 'preflight',
        errorRequestId: 'req-test-123',
      },
    })

    expect(wrapper.get('[role="alert"]').text()).toContain('internal_error')
    expect(wrapper.text()).toContain('阶段：preflight')
    expect(wrapper.text()).toContain('req-test-123')
  })

  it('keeps example selection and submits the current trimmed prompt', async () => {
    const wrapper = mount(QueryComposer, { props: { modelValue: '  查价格  ' } })

    await wrapper.get('form').trigger('submit')
    await wrapper.get('.example').trigger('click')

    expect(wrapper.emitted('submit')).toEqual([['查价格']])
    expect(wrapper.emitted('selectExample')?.[0]?.[0]).toContain('万以内')
  })

  it('keeps live capability limits available in a collapsed disclosure', async () => {
    const wrapper = mount(QueryComposer, {
      props: {
        modelValue: '',
        capabilities: {
          dataset_version: 'test', row_count: 10, fields: {}, energy_values: [],
          supported_filters: ['budget'], unsupported_filters: ['body_type'],
          data_time_range: { status: 'available', start: null, end: null },
        },
      },
    })

    expect(wrapper.get('.composer__capability summary').text()).toBe('能力与限制')
    expect((wrapper.get('.composer__capability').element as HTMLDetailsElement).open).toBe(false)
    await wrapper.get('.composer__capability summary').trigger('click')
    expect((wrapper.get('.composer__capability').element as HTMLDetailsElement).open).toBe(true)
    expect(wrapper.get('.composer__hint').text()).toContain('暂不支持：车型类别')
  })
})
