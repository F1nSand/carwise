const numberFormatter = new Intl.NumberFormat('en-US', { maximumFractionDigits: 1 })

function trimDecimal(value: number): string {
  return value.toFixed(1).replace(/\.0$/, '')
}

export function formatWan(value: number): string {
  return `${trimDecimal(value / 10000)} 万`
}

export function formatWanAmount(value: number): string {
  return `${trimDecimal(value)} 万`
}

export function formatNumber(value: number | null): string {
  return value === null ? '—' : numberFormatter.format(value)
}

export function formatMileageWanKm(value: number): string {
  return `${trimDecimal(value / 10000)} 万公里`
}

export function formatScore(value: number): string {
  return `${Math.round(value * 100)} 分`
}

export function formatDateTime(value: string): string {
  return value.replace(' ', ' · ')
}
