import React from 'react'

export function Badge({ value, kind = '' }) {
  if (value === null || value === undefined) return null
  return <span className={`badge ${kind || String(value)}`}>{String(value).replace(/_/g, ' ')}</span>
}

export function Banner({ kind = 'info', children, onDismiss }) {
  if (!children) return null
  return (
    <div className={`banner ${kind}`}>
      <div className="row">
        <div style={{ flex: 1 }}>{children}</div>
        {onDismiss && (
          <button className="small" onClick={onDismiss}>
            Dismiss
          </button>
        )}
      </div>
    </div>
  )
}

/**
 * Confidence is shown, not hidden, and low values are visually distinct — a
 * reviewer should be able to see at a glance which proposals the model was least
 * sure about and spend their attention there.
 */
export function Confidence({ value }) {
  const pct = Math.round((value ?? 0) * 100)
  return (
    <span className={`confidence ${pct < 50 ? 'low' : ''}`} title={`Model confidence: ${pct}%`}>
      <span className="bar">
        <i style={{ width: `${pct}%` }} />
      </span>
      {pct}%
    </span>
  )
}

export function Empty({ children }) {
  return <div className="empty">{children}</div>
}

export function Spinner({ label = 'Working…' }) {
  return (
    <span className="muted">
      <span className="spinner" /> {label}
    </span>
  )
}

/** Renders [REDACTED:KIND] placeholders as visible chips rather than raw text. */
export function RedactedText({ text }) {
  const parts = String(text || '').split(/(\[REDACTED:[A-Z_]+\])/g)
  return (
    <>
      {parts.map((part, i) =>
        part.startsWith('[REDACTED:') ? (
          <span key={i} className="redacted" title="Removed before storage by the PII scrubber">
            {part.slice(10, -1)} REDACTED
          </span>
        ) : (
          <React.Fragment key={i}>{part}</React.Fragment>
        )
      )}
    </>
  )
}

export function formatTime(iso) {
  if (!iso) return '—'
  return new Date(iso).toLocaleString()
}

export function formatDuration(ms) {
  if (ms === null || ms === undefined) return '—'
  return ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(2)} s`
}
