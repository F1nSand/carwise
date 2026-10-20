import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import ChartCard from './ChartCard.vue'

const echarts = vi.hoisted(() => ({
  use: vi.fn(),
  init: vi.fn(),
  setOption: vi.fn(),
  resize: vi.fn(),
  dispose: vi.fn(),
}))
vi.mock('echarts/core', () => ({ use: echarts.use, init: echarts.init }))
vi.mock('echarts/charts', () => ({ BarChart: {}, LineChart: {} }))
vi.mock('echarts/components', () => ({ GridComponent: {}, TooltipComponent: {} }))
vi.mock('echarts/renderers', () => ({ SVGRenderer: {} }))

const baseProps = { title: '车系价格对比', unit: '万元', summary: '价格对比' }

beforeEach(() => {
  vi.clearAllMocks()
  echarts.init.mockReturnValue({ setOption: echarts.setOption, resize: echarts.resize, dispose: echarts.dispose })
})

describe('ChartCard', () => {
  it('initializes a real chart instance, updates it, and disposes on unmount', async () => {
    const wrapper = mount(ChartCard, { props: { ...baseProps, series: [{ label: '宝马3系', value: 10.99 }] } })
    await flushPromises()

    expect(wrapper.find('.chart').exists()).toBe(true)
    expect(echarts.init).toHaveBeenCalledTimes(1)
    expect(echarts.setOption).toHaveBeenCalledWith(expect.objectContaining({ xAxis: expect.objectContaining({ data: ['宝马3系'] }) }))
    expect(echarts.resize).toHaveBeenCalled()

    await wrapper.setProps({ series: [{ label: '奥迪 A4L', value: 14 }] })
    await flushPromises()
    expect(echarts.init).toHaveBeenCalledTimes(2)
    expect(echarts.dispose).toHaveBeenCalledTimes(1)
    expect(echarts.setOption).toHaveBeenLastCalledWith(expect.objectContaining({ xAxis: expect.objectContaining({ data: ['奥迪 A4L'] }) }))

    wrapper.unmount()
    expect(echarts.dispose).toHaveBeenCalledTimes(2)
  })

  it('disconnects the chart while showing the data table and restores it when closed', async () => {
    const wrapper = mount(ChartCard, { props: { ...baseProps, series: [{ label: '宝马3系', value: 10.99 }] } })
    await flushPromises()
    await wrapper.get('.table-toggle').trigger('click')
    await flushPromises()
    expect(wrapper.find('.chart').exists()).toBe(false)
    expect(wrapper.find('table').exists()).toBe(true)
    expect(echarts.dispose).toHaveBeenCalledTimes(1)

    await wrapper.get('.table-toggle').trigger('click')
    await flushPromises()
    expect(echarts.init).toHaveBeenCalledTimes(2)
    wrapper.unmount()
  })

  it('shows an accessible empty state when chart data is empty', () => {
    const wrapper = mount(ChartCard, { props: { ...baseProps, series: [] } })
    expect(wrapper.find('[data-chart-empty]').exists()).toBe(true)
    expect(wrapper.text()).toContain('暂无可展示数据')
    expect(echarts.init).not.toHaveBeenCalled()
  })
})
