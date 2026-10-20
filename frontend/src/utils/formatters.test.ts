import { describe, expect, it } from 'vitest'
import { formatMileageWanKm, formatNumber, formatWan } from './formatters'

describe('formatters', () => {
  it('formats prices in ten-thousand-yuan units', () => {
    expect(formatWan(148000)).toBe('14.8 万')
  })

  it('formats counts with locale separators', () => {
    expect(formatNumber(143793)).toBe('143,793')
  })

  it('formats mileage in ten-thousand-kilometre units', () => {
    expect(formatMileageWanKm(41000)).toBe('4.1 万公里')
  })
})
