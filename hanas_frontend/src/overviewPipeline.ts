export type OverviewPipelineStage =
  | 'orchestrator_agent'
  | 'monitoring_agent'
  | 'diagnostic_reasoning_agent'
  | 'decision_agent'
  | 'dose_planning_agent'
  | 'consistency_review'
  | 'safety_gate'
  | 'human_review_gate'

export type OverviewPipelineDisplayState =
  | 'complete'
  | 'passed'
  | 'waiting'
  | 'active'
  | 'queued'
  | 'pending'
  | 'skipped'

export function overviewPipelineStagePresentation({
  stage,
  index,
  isCollectionRow,
  activeIndex,
  skipped,
  waitingForConfirmation,
}: {
  stage: OverviewPipelineStage
  index: number
  isCollectionRow: boolean
  activeIndex: number
  skipped: boolean
  waitingForConfirmation: boolean
}): { status: OverviewPipelineDisplayState; label: string } {
  if (isCollectionRow) {
    if (index === 0) return { status: 'complete', label: 'Captured' }
    if (index === 1) return { status: 'queued', label: 'Next batch' }
    return { status: 'pending', label: 'Batch only' }
  }

  if (skipped) return { status: 'skipped', label: 'Skipped' }
  if (waitingForConfirmation && stage === 'monitoring_agent') {
    return { status: 'waiting', label: 'Waiting' }
  }

  if (activeIndex >= 0) {
    if (index === activeIndex) return { status: 'active', label: 'Running' }
    if (index > activeIndex) return { status: 'pending', label: 'Not reached' }
  }

  if (stage === 'consistency_review' || stage === 'safety_gate') {
    return { status: 'passed', label: 'Passed' }
  }
  if (stage === 'human_review_gate') return { status: 'passed', label: 'Clear' }
  return { status: 'complete', label: 'Completed' }
}
