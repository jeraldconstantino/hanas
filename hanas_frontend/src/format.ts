export function formatDoseMl(value: number): string {
  if (!Number.isFinite(value)) return '—'
  const amount = new Intl.NumberFormat('en-US', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(value)
  return `${amount} mL`
}

export function formatVersionLabel(value: string): string {
  const version = value.trim()
  if (!version) return '—'
  if (/^v\d/i.test(version)) return version
  if (/^\d/.test(version)) return `v${version}`
  return version
}
