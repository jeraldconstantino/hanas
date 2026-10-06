import { memo } from 'react'
import type { Tone } from '../../types'
import { Pill } from './Pill'
import { formatDisplayText, formatSentenceText } from '../../text'

const WARN_CELLS = new Set([
  'dosing', 'Pending mix', 'In progress', 'mixing', 'confirming', 'unstable',
  'wait_human_review', 'human_override_pending_execution', 'suppressed',
  'wait_for_mixing', 'wait_initial_confirmation', 'wait_for_stability',
  'batch_expired', 'expired', 'wait_near_boundary',
])

const GOOD_CELLS = new Set([
  'completed', 'Resolved', 'within_range', 'Recovered', 'within_control_tolerance',
  'human_approved_pending_execution', 'sent',
])

const DANGER_CELLS = new Set([
  'ph_high', 'ph_low', 'ec_high', 'ec_low', 'sensor_anomaly', 'failed', 'error',
  'emergency_stop', 'emergency_stopped',
])

const INFO_CELLS = new Set([
  'human_command_dispatched', 'waiting', 'confirming', 'batch_pending',
  'batch_dispatched', 'pending', 'queued', 'maintenance_mode',
  'monitoring_mode', 'monitoring_cancelled',
])

const NEUTRAL_CELLS = new Set([
  'human_rejected', 'disabled', 'no_action', 'within_range', 'within_control_tolerance',
  'none', 'not_sent', 'skipped', 'logged',
])

function toneForTableCell(cell: string, key: string): Tone {
  if (WARN_CELLS.has(cell) || WARN_CELLS.has(key)) return 'warn'
  if (GOOD_CELLS.has(cell) || GOOD_CELLS.has(key)) return 'good'
  if (DANGER_CELLS.has(cell) || DANGER_CELLS.has(key)) return 'danger'
  if (INFO_CELLS.has(cell) || INFO_CELLS.has(key)) return 'info'
  return 'neutral'
}

function renderTableCell(cell: string, header: string) {
  const headerKey = header.trim().toLowerCase()
  if (['time', 'started', 'cycle', 'ph', 'ec', 'dose', 'duration', 'flow rate', 'dose factor', 'max dose / cycle', 'max duration'].includes(headerKey)) {
    return cell
  }
  if (headerKey === 'pump') {
    return cell.trim().toLowerCase() === 'none' ? 'None' : formatDisplayText(cell)
  }

  const key = cell.trim().toLowerCase().replace(/[\s-]+/g, '_')
  const label = formatDisplayText(cell)
  const tone = toneForTableCell(cell, key)

  if (headerKey === 'action state' || headerKey === 'reason') {
    return (
      <span className={`table-status-text ${headerKey === 'action state' ? 'action' : 'reason'} ${tone}`}>
        {label}
      </span>
    )
  }

  if (WARN_CELLS.has(cell) || WARN_CELLS.has(key)) return <Pill label={label} tone="warn" />
  if (GOOD_CELLS.has(cell) || GOOD_CELLS.has(key)) return <Pill label={label} tone="good" />
  if (DANGER_CELLS.has(cell) || DANGER_CELLS.has(key)) return <Pill label={label} tone="danger" />
  if (INFO_CELLS.has(cell) || INFO_CELLS.has(key)) return <Pill label={label} tone="info" />
  if (NEUTRAL_CELLS.has(cell) || NEUTRAL_CELLS.has(key)) return <Pill label={label} tone="neutral" />
  return label
}

export const DataTable = memo(function DataTable({ headers, rows }: { headers: string[]; rows: string[][] }) {
  const tableLabel = `${headers.map(formatSentenceText).join(', ')} data table`

  return (
    <div className="table-wrap" role="region" aria-label={tableLabel} tabIndex={0}>
      <table>
        <thead>
          <tr>{headers.map((h) => <th key={h} scope="col">{formatSentenceText(h)}</th>)}</tr>
        </thead>
        <tbody>
          {rows.map((row, rowIndex) => (
            <tr key={`${row[0]}-${rowIndex}`}>
              {row.map((cell, cellIndex) => (
                <td
                  key={`${cell}-${cellIndex}`}
                  data-label={formatSentenceText(headers[cellIndex] ?? '')}
                >
                  {renderTableCell(cell, headers[cellIndex] ?? '')}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
})
