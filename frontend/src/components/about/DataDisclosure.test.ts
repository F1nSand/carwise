import { mount, flushPromises } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({ getDataCapabilities: vi.fn() }))
vi.mock('../../services/analysisAdapter', () => ({ getDataCapabilities: api.getDataCapabilities }))

import DataDisclosure from './DataDisclosure.vue'

const capabilities = {
  dataset_version: 'snapshot-test-7',
  row_count: 143793,
  fields: {
    price_yuan: { coverage: 1 },
    mileage_km: { coverage: 1 },
    production_year: { coverage: 1 },
    vehicle_age_years: { coverage: 0.9935 },
    city_name: { coverage: 0.9996 },
    brand_name: { coverage: 1 },
    energy_type: { coverage: 0.0857 },
    body_type: { coverage: 0 },
    drive_type: { coverage: 0 },
  },
  energy_values: ['汽油', '纯电动'],
  supported_filters: ['budget', 'city', 'brand', 'series', 'year', 'energy_type', 'max_age'],
  unsupported_filters: ['body_type', 'drive_type'],
  data_time_range: { status: 'unknown', start: null, end: null },
}

describe('DataDisclosure', () => {
  beforeEach(() => { api.getDataCapabilities.mockReset() })

  it('renders data returned by the live capabilities contract', async () => {
    api.getDataCapabilities.mockResolvedValue(capabilities)
    const wrapper = mount(DataDisclosure)
    await flushPromises()

    expect(wrapper.text()).toContain('143,793')
    expect(wrapper.get('code').attributes('title')).toBe('snapshot-test-7')
    expect(wrapper.text()).toContain('挂牌价格')
    expect(wrapper.text()).toContain('记录车龄')
    expect(wrapper.text()).not.toContain('price_yuan')
    expect(wrapper.text()).toContain('8.57%')
    expect(wrapper.text()).toContain('汽油、纯电动')
  })

  it('shows a useful error and retries the capabilities request', async () => {
    api.getDataCapabilities.mockRejectedValueOnce(new Error('服务暂不可用'))
      .mockResolvedValueOnce(capabilities)
    const wrapper = mount(DataDisclosure)
    await flushPromises()

    expect(wrapper.get('[role="alert"]').text()).toContain('服务暂不可用')
    await wrapper.get('button').trigger('click')
    await flushPromises()

    expect(api.getDataCapabilities).toHaveBeenCalledTimes(2)
    expect(wrapper.text()).toContain('143,793')
  })

  it('states unknown time and explains low coverage without inferring fuel type or accuracy', async () => {
    api.getDataCapabilities.mockResolvedValue(capabilities)
    const wrapper = mount(DataDisclosure)
    await flushPromises()

    expect(wrapper.text()).toContain('数据采集时间未知')
    expect(wrapper.text()).toContain('能源字段未填写时只表示信息缺失')
    expect(wrapper.text()).toContain('覆盖率也不代表数据准确率')
    expect(wrapper.get('[aria-label="车型类别覆盖 0.00%"]')).toBeTruthy()
    expect(wrapper.get('[aria-label="车源城市覆盖 99.96%"]')).toBeTruthy()
  })
})
