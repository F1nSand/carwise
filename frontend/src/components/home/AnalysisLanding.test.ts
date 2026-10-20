import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import AnalysisLanding from './AnalysisLanding.vue'

const api = vi.hoisted(() => ({ getDataCapabilities: vi.fn() }))
vi.mock('../../services/analysisAdapter', () => ({ getDataCapabilities: api.getDataCapabilities }))
vi.mock('./HomeMarketLens.vue', () => ({
  default: { props: ['capabilities', 'capabilitiesLoaded'], emits: ['select'], template: '<section data-testid="home-market-lens"></section>' },
}))

const capabilities = {
  dataset_version: 'snapshot-test', row_count: 2574, fields: {}, energy_values: [],
  supported_filters: ['budget', 'city', 'series', 'year', 'max_age'], unsupported_filters: [],
  data_time_range: { status: 'available', start: null, end: null },
}

let wrapper: VueWrapper

beforeEach(() => {
  vi.clearAllMocks()
  api.getDataCapabilities.mockResolvedValue(capabilities)
})
afterEach(() => wrapper?.unmount())

describe('AnalysisLanding', () => {
  it('offers starters from the supported filters and fills the draft without submitting', async () => {
    wrapper = mount(AnalysisLanding, {
      props: { modelValue: '' },
      global: { stubs: { RouterLink: { props: ['to'], template: '<a :href="to"><slot /></a>' } } },
    })
    await flushPromises()

    const starters = wrapper.findAll('.capability')
    expect(starters).toHaveLength(5)
    await starters[0].trigger('click')

    expect(wrapper.emitted('update:modelValue')?.[0]?.[0]).toContain('按预算筛选')
    expect(wrapper.emitted('submit')).toBeUndefined()
    expect(wrapper.get('.analysis-preview__record strong').text()).toContain('2,574')
  })

  it('does not invent a record count when the capabilities request fails', async () => {
    api.getDataCapabilities.mockRejectedValueOnce(new Error('unavailable'))
    wrapper = mount(AnalysisLanding, {
      props: { modelValue: '' },
      global: { stubs: { RouterLink: { props: ['to'], template: '<a :href="to"><slot /></a>' } } },
    })
    await flushPromises()

    expect(wrapper.get('.analysis-preview__record strong').text()).toBe('暂不可读取')
    expect(wrapper.text()).not.toContain('143,793')
  })

  it('exposes the composer rectangle for the chat transition without duplicating global navigation', () => {
    wrapper = mount(AnalysisLanding, {
      props: { modelValue: '' },
      global: { stubs: { RouterLink: { props: ['to'], template: '<a :href="to"><slot /></a>' } } },
    })

    expect(wrapper.vm.getComposerRect).toBeTypeOf('function')
    expect(wrapper.find('a').exists()).toBe(false)
    expect(wrapper.get('.analysis-preview__cta').text()).toContain('开始一次分析')
  })

  it('keeps the hero action connected to the real query composer', async () => {
    wrapper = mount(AnalysisLanding, { props: { modelValue: '' }, attachTo: document.body })

    await wrapper.get('.analysis-preview__cta').trigger('click')

    expect(wrapper.get('textarea').element).toBe(document.activeElement)
    expect(wrapper.emitted('submit')).toBeUndefined()
  })
})
