import { flushPromises, mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import RunCorrectionForm from './RunCorrectionForm.vue'

describe('RunCorrectionForm', () => {
  it('trims and emits a correction and clears it after the Agent applies it', async () => {
    const wrapper = mount(RunCorrectionForm, {
      props: { busy: false, status: 'idle', error: '' },
    })
    await wrapper.get('textarea').setValue('  预算改为 12 万以内  ')
    await wrapper.get('form').trigger('submit')

    expect(wrapper.emitted('submit')?.[0]).toEqual(['预算改为 12 万以内'])

    await wrapper.setProps({ status: 'applied' })
    await flushPromises()
    expect((wrapper.get('textarea').element as HTMLTextAreaElement).value).toBe('')
    expect(wrapper.get('[role="status"]').text()).toBe('已采用')
  })

  it('does not emit empty or over-limit content and exposes submission errors', async () => {
    const wrapper = mount(RunCorrectionForm, {
      props: { busy: false, status: 'error', error: '运行已经结束' },
    })
    await wrapper.get('form').trigger('submit')
    await wrapper.get('textarea').setValue('很长'.repeat(251))
    await wrapper.get('form').trigger('submit')

    expect(wrapper.emitted('submit')).toBeUndefined()
    expect(wrapper.get('[role="alert"]').text()).toBe('运行已经结束')
  })
})
