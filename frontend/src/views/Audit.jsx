import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'
import { Badge, Banner, Empty, Spinner, formatTime } from '../components/common'

const ACTOR_KIND = { human: 'approved', agent: 'uploaded', system: 'proposed' }

/**
 * Audit trail viewer.
 *
 * Read-only by construction: the API exposes no way to edit or delete an event,
 * and neither does this page. Filtering is client-side over a fetched page so a
 * reviewer can narrow to gates or denials without losing the full sequence.
 */
export default function Audit() {
  const [jobs, setJobs] = useState([])
  const [jobId, setJobId] = useState('')
  const [events, setEvents] = useState([])
  const [filter, setFilter] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    api.listJobs().then((list) => {
      setJobs(list)
      if (list.length > 0) setJobId(list[0].id)
    }).catch((err) => setError(err.message))
  }, [])

  const load = useCallback(async () => {
    if (!jobId) return
    setLoading(true)
    try {
      setEvents(await api.getAudit(jobId))
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }, [jobId])

  useEffect(() => { load() }, [load])

  const shown = filter
    ? events.filter((e) => e.action.toLowerCase().includes(filter.toLowerCase()))
    : events

  const denials = events.filter((e) => e.outcome === 'denied').length

  return (
    <>
      <Banner kind="error" onDismiss={() => setError('')}>{error}</Banner>

      <div className="card">
        <h2>Audit trail</h2>
        <p className="hint">
          Append-only record of every orchestrator step, tool call, human decision and export.
          There is no edit or delete path, here or in the API.
        </p>

        <div className="row" style={{ marginBottom: 14 }}>
          <div style={{ flex: 2, minWidth: 220 }}>
            <label>Meeting</label>
            <select value={jobId} onChange={(e) => setJobId(e.target.value)}>
              {jobs.map((j) => (
                <option key={j.id} value={j.id}>{j.title || j.original_filename}</option>
              ))}
            </select>
          </div>
          <div style={{ flex: 1, minWidth: 160 }}>
            <label>Filter by action</label>
            <input value={filter} onChange={(e) => setFilter(e.target.value)} placeholder="gate, tool, review…" />
          </div>
          <div style={{ alignSelf: 'flex-end' }}>
            <button onClick={load} disabled={loading}>Refresh</button>
          </div>
          {jobId && (
            <div style={{ alignSelf: 'flex-end' }}>
              <a className="muted" href={`/api/v1/jobs/${jobId}/audit/export`}>Export JSONL</a>
            </div>
          )}
        </div>

        <p className="muted">
          {events.length} event(s){denials > 0 && ` · ${denials} refused action(s) recorded`}
        </p>

        {loading ? (
          <Spinner label="Loading trail…" />
        ) : shown.length === 0 ? (
          <Empty>No audit events match.</Empty>
        ) : (
          <table>
            <thead>
              <tr>
                <th>When</th>
                <th>Actor</th>
                <th>Action</th>
                <th>Outcome</th>
                <th>Detail</th>
              </tr>
            </thead>
            <tbody>
              {shown.map((e) => (
                <tr key={e.id}>
                  <td style={{ whiteSpace: 'nowrap' }}>{formatTime(e.created_at)}</td>
                  <td><Badge value={e.actor_type} kind={ACTOR_KIND[e.actor_type]} /></td>
                  <td className="mono">{e.action}</td>
                  <td>
                    {e.outcome === 'success'
                      ? <span className="muted">ok</span>
                      : <Badge value={e.outcome} kind={e.outcome === 'denied' ? 'rejected' : 'pending'} />}
                  </td>
                  <td>
                    <details>
                      <summary className="muted">
                        {e.resource_type || '—'}
                        {e.duration_ms != null && ` · ${Math.round(e.duration_ms)} ms`}
                      </summary>
                      <pre className="mono" style={{ whiteSpace: 'pre-wrap', margin: '6px 0 0' }}>
                        {JSON.stringify(e.detail, null, 2)}
                      </pre>
                    </details>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </>
  )
}
