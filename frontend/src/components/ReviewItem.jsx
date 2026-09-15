import { useEffect, useRef, useState } from 'react'
import { Badge, Confidence } from './common'

/**
 * One extracted decision or action item, with its review controls.
 *
 * The component times how long the reviewer has the item open and sends that as
 * `review_seconds`. That figure is the reviewer-correction-time metric in the
 * evaluation dossier, so it is measured here rather than estimated later: the
 * clock starts when the item first renders and is read at the moment of ruling.
 */
export default function ReviewItem({ item, type, onReview, busy }) {
  const [editing, setEditing] = useState(false)
  const [text, setText] = useState(item.text)
  const [owner, setOwner] = useState(item.owner_name || '')
  const [deadline, setDeadline] = useState(item.deadline || '')
  const [note, setNote] = useState('')
  const openedAt = useRef(Date.now())

  useEffect(() => {
    openedAt.current = Date.now()
  }, [item.id])

  const elapsed = () => Number(((Date.now() - openedAt.current) / 1000).toFixed(1))
  const reviewed = item.status !== 'proposed'

  const submit = (status) => {
    const payload = { status, review_seconds: elapsed(), note: note || null }
    if (status === 'edited') {
      payload.text = text
      if (type === 'action') {
        payload.owner_name = owner
        payload.deadline = deadline
      }
    }
    onReview(item.id, payload)
    setEditing(false)
  }

  return (
    <div className={`item ${reviewed ? 'reviewed' : ''}`}>
      <div className="item-head">
        <div className="text">
          {editing ? (
            <textarea value={text} onChange={(e) => setText(e.target.value)} rows={3} />
          ) : (
            item.text
          )}
        </div>
        <Badge value={item.status} />
      </div>

      {type === 'action' && (
        <div className="meta">
          {editing ? (
            <>
              <div style={{ flex: 1, minWidth: 160 }}>
                <label>Owner</label>
                <input
                  value={owner}
                  onChange={(e) => setOwner(e.target.value)}
                  placeholder="Unassigned"
                />
              </div>
              <div style={{ flex: 1, minWidth: 160 }}>
                <label>Deadline</label>
                <input
                  value={deadline}
                  onChange={(e) => setDeadline(e.target.value)}
                  placeholder="e.g. 2026-10-05 or 'by Friday'"
                />
              </div>
            </>
          ) : (
            <>
              <span>
                <strong>Owner:</strong> {item.owner_name || <em>unassigned</em>}
              </span>
              <span>
                <strong>Deadline:</strong> {item.deadline || <em>none</em>}
              </span>
            </>
          )}
        </div>
      )}

      <div className="meta">
        <Confidence value={item.confidence} />
        <span className="mono" title="Which extractor produced this item">
          {item.source_tool}
        </span>
        {item.review_seconds != null && <span>reviewed in {item.review_seconds}s</span>}
      </div>

      {item.evidence_quote && (
        <div className="quote" title="Verbatim span from the transcript that this item came from">
          “{item.evidence_quote}”
        </div>
      )}

      {item.original_text && item.original_text !== item.text && (
        <div className="muted">
          <strong>Model originally proposed:</strong> {item.original_text}
        </div>
      )}

      {!reviewed && (
        <>
          {editing && (
            <div style={{ marginTop: 10 }}>
              <label>Note (optional — why you changed it)</label>
              <input value={note} onChange={(e) => setNote(e.target.value)} />
            </div>
          )}
          <div className="actions">
            {editing ? (
              <>
                <button className="ok" disabled={busy} onClick={() => submit('edited')}>
                  Save correction
                </button>
                <button
                  disabled={busy}
                  onClick={() => {
                    setEditing(false)
                    setText(item.text)
                  }}
                >
                  Cancel
                </button>
              </>
            ) : (
              <>
                <button className="ok" disabled={busy} onClick={() => submit('approved')}>
                  Accept
                </button>
                <button disabled={busy} onClick={() => setEditing(true)}>
                  Correct
                </button>
                <button className="danger" disabled={busy} onClick={() => submit('rejected')}>
                  Reject
                </button>
              </>
            )}
          </div>
        </>
      )}

      {reviewed && item.review_note && (
        <div className="muted" style={{ marginTop: 8 }}>
          <strong>Reviewer note:</strong> {item.review_note}
        </div>
      )}
    </div>
  )
}
