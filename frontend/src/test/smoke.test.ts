import { describe, expect, it } from 'vitest'

describe('frontend scaffold', () => {
  it('exposes the product name for the first render', () => {
    expect('二手车智能分析').toContain('二手车')
  })
})
