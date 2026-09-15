import { useState } from 'react'
import { Badge } from './common'

/**
 * A pending approval gate.
 *
 * The full payload is shown, not summarised away: a reviewer is being asked to
 * authorise a specific action, so they must be able to see exactly what will run.
 * Approving a summary they cannot inspect would make the gate theatre.
 */
export default function ApprovalGate({ gate, onDecide, busy }) {
  const [note, setNote] = useState('')
  const [showPayload, setShowPayload] = useState(false)

  const payload = gate.payload || {}
  const decisionCount = payload.decisions?.length
  const actionCount = payload.actions?.length

  return (
    <div className="gate">
      <div className="row" style={{ marginBottom: 8 }}>
        <h3 style={{ flex: 1 }}>Approval required — {gate.action.replace(/_/g, ' ')}</h3>
        <Badge value={gate.risk} kind={`risk-${gate.risk}`} />
      </div>

      <p style={{ margin: '0 0 10px' }}>{gate.summary}</p>

      {(decisionCount != null || actionCount != null) && (
        <p className="muted" style={{ margin: '0 0 10px' }}>
          {decisionCount ?? 0} decision(s) and {actionCount ?? 0} action item(s) will be written.
        </p>
      )}

      {payload.format && (
        <p className="muted" style={{ margin: '0 0 10px' }}>
          Format: <strong>{String(payload.format).toUpperCase()}</strong> · Scope:{' '}
          <strong>
            {payload.include_unapproved
              ? 'all non-rejected items'
              : 'reviewer-approved items only'}
          </strong>
        </p>
      )}

      <button className="small" onClick={() => setShowPayload((v) => !v)}>
        {showPayload ? 'Hide' : 'Inspect'} exact payload
      </button>

      {showPayload && <pre>{JSON.stringify(payload, null, 2)}</pre>}

      <div style={{ marginTop: 12 }}>
        <label>Note (recorded in the audit trail)</label>
        <input
          value={note}
          onChange={(e) => setNote(e.target.value)}
          placeholder="e.g. Checked items 2 and 4 against the recording"
        />
      </div>

      <div className="actions" style={{ marginTop: 12, display: 'flex', gap: 8 }}>
        <button className="ok" disabled={busy} onClick={() => onDecide(gate.id, 'approved', note)}>
          Approve
        </button>
        <button
          className="danger"
          disabled={busy}
          onClick={() => onDecide(gate.id, 'rejected', note)}
        >
          Reject
        </button>
      </div>

      <p className="muted" style={{ marginTop: 10, marginBottom: 0 }}>
        Nothing is written or exported until you decide. Your name, the time and this note are
        recorded permanently.
      </p>
    </div>
  )
}
