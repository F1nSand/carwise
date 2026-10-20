import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import SearchMultiSelect from './SearchMultiSelect.vue'

describe('SearchMultiSelect', () => {
  it('filters choices while retaining selected options and supports independent checkbox selection', async () => {
    const wrapper = mount(SearchMultiSelect, { props: { modelValue: ['品牌甲'], options: ['品牌甲', '品牌乙', '长安汽车'], label: '品牌', 'onUpdate:modelValue': (value: string[]) => wrapper.setProps({ modelValue: value }) } })
    await wrapper.get('input[type="search"]').setValue('长安')
    expect(wrapper.findAll('.choice')).toHaveLength(1)
    expect(wrapper.text()).toContain('品牌甲')
    await wrapper.get('.choice input').setValue(true)
    expect(wrapper.props('modelValue')).toEqual(['品牌甲', '长安汽车'])
  })
})
