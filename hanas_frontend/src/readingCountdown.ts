export function nextReadingCountdown(latestMs: number, referenceMs: number, samplingSeconds = 60) {
  if (!Number.isFinite(latestMs) || !Number.isFinite(referenceMs) || samplingSeconds <= 0) {
    return { remainingSeconds: Math.max(1, samplingSeconds), progress: 0 }
  }
  const elapsedSeconds = Math.max(0, Math.floor((referenceMs - latestMs) / 1000))
  const elapsedInCycle = elapsedSeconds % samplingSeconds
  return {
    remainingSeconds: samplingSeconds - elapsedInCycle,
    progress: Math.min(100, Math.max(0, (elapsedInCycle / samplingSeconds) * 100)),
  }
}
