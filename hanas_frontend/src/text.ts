const ACRONYMS: Record<string, string> = {
  ai: 'AI',
  api: 'API',
  ec: 'EC',
  esp32: 'ESP32',
  hitl: 'HITL',
  llm: 'LLM',
  ph: 'pH',
  sms: 'SMS',
  dft: 'DFT',
  ads1115: 'ADS1115',
  min: 'min',
  hr: 'hr',
  s: 's',
}

const EXACT_LABELS: Record<string, string> = {
  agentic_ai: 'Agentic AI',
  baseline: 'Baseline',
  within_range: 'Within Range',
  ph: 'pH',
  ec: 'EC',
  llm: 'LLM',
  ads1115: 'ADS1115',
  ml: 'mL',
  'ml/min': 'mL/min',
  'ml/l/unit': 'mL/L/unit',
  ms: 'ms',
  'ms/cm': 'mS/cm',
  'mS/cm': 'mS/cm',
}

const LOWERCASE_WORDS = new Set(['a', 'an', 'and', 'as', 'at', 'by', 'for', 'in', 'of', 'on', 'or', 'per', 'the', 'to', 'vs'])

export function normalizeUnits(text: string): string {
  return text
    .replace(/(\d(?:[\d,.]*\d)?)ml\b/gi, '$1 mL')
    .replace(/(\d(?:[\d,.]*\d)?)ms\b/g, '$1 ms')
    .replace(/(\d(?:[\d,.]*\d)?)s\b/g, '$1 s')
    .replace(/temp=([\d.]+)C\b/gi, 'temp=$1°C')
    .replace(/\bph\b/gi, 'pH')
    .replace(/\bMl\b/g, 'mL')
    .replace(/\bML\b/g, 'mL')
    .replace(/\bMs\b/g, 'ms')
    .replace(/\bMsec\b/g, 'ms')
    .replace(/\bMl\/min\b/g, 'mL/min')
    .replace(/\bML\/min\b/g, 'mL/min')
    .replace(/\bMl\/l\/unit\b/gi, 'mL/L/unit')
    .replace(/\bms\/cm\b/gi, 'mS/cm')
}

function capitalizeWord(word: string, index: number): string {
  const lower = word.toLowerCase()
  if (ACRONYMS[lower]) return ACRONYMS[lower]
  if (EXACT_LABELS[lower]) return EXACT_LABELS[lower]
  if (index > 0 && LOWERCASE_WORDS.has(lower)) return lower
  if (/^\d/.test(word)) return word
  return `${lower.charAt(0).toUpperCase()}${lower.slice(1)}`
}

export function formatDisplayText(value: string): string {
  const trimmed = value.trim()
  if (!trimmed) return value
  if (EXACT_LABELS[trimmed]) return EXACT_LABELS[trimmed]
  if (EXACT_LABELS[trimmed.toLowerCase()]) return EXACT_LABELS[trimmed.toLowerCase()]
  if (/^[\d\s.,:+/%°#-]+$/.test(trimmed)) return normalizeUnits(trimmed)

  return normalizeUnits(trimmed
    .replace(/_/g, ' ')
    .replace(/\s+/g, ' ')
    .split(' ')
    .map(capitalizeWord)
    .join(' '))
}

/** Formats backend-style identifiers when they appear inside operator-facing prose. */
export function formatEmbeddedDisplayText(value: string): string {
  return normalizeUnits(value.replace(
    /\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b/gi,
    (token) => formatDisplayText(token.toLowerCase()),
  ))
}

export function formatSentenceText(value: string): string {
  const normalized = value.trim().replace(/_/g, ' ').replace(/\s+/g, ' ')
  if (!normalized) return value

  return normalizeUnits(normalized
    .split(' ')
    .map((word, index) => {
      const lower = word.toLowerCase()
      if (ACRONYMS[lower]) return ACRONYMS[lower]
      if (EXACT_LABELS[lower]) return EXACT_LABELS[lower]
      if (/^[\d#]/.test(word)) return word
      return index === 0 ? `${lower.charAt(0).toUpperCase()}${lower.slice(1)}` : lower
    })
    .join(' '))
}

export function formatStrategyLabel(value: string | null | undefined): string {
  if (!value) return '—'
  if (value === 'agentic_ai') return 'Agentic AI'
  if (value === 'baseline') return 'Baseline'
  return formatDisplayText(value)
}

export function shouldShowBatchRunStatus(status: string): boolean {
  return status.trim().toLowerCase() !== 'completed'
}
