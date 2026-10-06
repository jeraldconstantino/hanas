// Three missed one-minute samples require historical wording.
export function isReadingStale(timestamp: string, now = Date.now()): boolean {
  const recorded = Date.parse(timestamp)
  return !Number.isFinite(recorded) || now - recorded > 180_000 || recorded - now > 60_000
}
export function recordedDate(timestamp: string): string {
  const date = new Date(timestamp)
  return Number.isFinite(date.getTime()) ? date.toLocaleString() : 'Time unavailable'
}
