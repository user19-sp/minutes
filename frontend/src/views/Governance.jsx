import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'
import ApprovalGate from '../components/ApprovalGate'
import { Badge, Banner, Empty, Spinner, formatTime } from '../components/common'

/**
 * The governance page makes the agent's capability surface legible.
 *
 * Everything here is read from the running system rather than written by hand, so
 * it cannot drift from what the code actually permits: if a tool is added, it
 * appears here.
 */
export default function Governance() {
  const [tools, setTools] = useState([])
  const [policy, setPolicy] = useState(null)
  const [queue, setQueue] = useState([])
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(false)
  const [loading, setLoading] = useState(true)

  const load = useCallback(async () => {
    try {
      const [t, p, q] = await Promise.all([
        api.listTools(),
        api.getPolicy(),
        api.listApprovals('pending'),
      ])
      setTools(t)
      setPolicy(p)
      setQueue(q)
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  const decide = async (id, decision, note) => {
    setBusy(true)
    try {
      await api.decideApproval(id, decision, note)
      setNotice(decision === 'approved' ? 'Approved — the action has run.' : 'Rejected — nothing was written.')
      await load()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  if (loading) return <Spinner label="Loading governance configuration…" />

  return (
    <>
      <Banner kind="error" onDismiss={() => setError('')}>{error}</Banner>
      <Banner kind="ok" onDismiss={() => setNotice('')}>{notice}</Banner>

      <div className="card">
        <h2>Approval queue</h2>
        <p className="hint">
          Actions waiting for a human decision. Nothing behind a gate has run.
        </p>
        {queue.length === 0 ? (
          <Empty>Nothing is waiting for approval.</Empty>
        ) : (
          queue.map((gate) => (
            <ApprovalGate key={gate.id} gate={gate} busy={busy} onDecide={decide} />
          ))
        )}
      </div>

      <div className="card">
        <h2>Tool allow-list</h2>
        <p className="hint">
          The complete set of actions the orchestrator can take. It has no general-purpose
          capability — no shell, no arbitrary queries, no outbound network calls. Anything not
          on this list is refused and recorded.
        </p>
        <table>
          <thead>
            <tr>
              <th>Tool</th>
              <th>Effect</th>
              <th>Human gate</th>
              <th>Max calls / run</th>
              <th>Risk</th>
            </tr>
          </thead>
          <tbody>
            {tools.map((tool) => (
              <tr key={tool.name}>
                <td>
                  <strong className="mono">{tool.name}</strong>
                  <div className="muted">{tool.description}</div>
                </td>
                <td>{tool.side_effect}</td>
                <td>
                  {tool.requires_approval ? (
                    <Badge value="required" kind="pending" />
                  ) : (
                    <span className="muted">not needed (read-only)</span>
                  )}
                </td>
                <td>{tool.max_calls_per_run}</td>
                <td>
                  <Badge value={tool.risk} kind={`risk-${tool.risk}`} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {policy && (
        <div className="card">
          <h2>Policy in force</h2>
          <table>
            <tbody>
              <tr>
                <th>Tool-call budget per run</th>
                <td>{policy.max_tool_calls_per_run}</td>
              </tr>
              <tr>
                <th>Auto-approval</th>
                <td>
                  {policy.auto_approve_enabled ? (
                    <Badge value="enabled" kind="rejected" />
                  ) : (
                    <Badge value="disabled — every gate needs a human" kind="approved" />
                  )}
                </td>
              </tr>
              <tr>
                <th>Gated tools</th>
                <td className="mono">{policy.gated_tools.join(', ')}</td>
              </tr>
              <tr>
                <th>Read-only tools</th>
                <td className="mono">{policy.ungated_read_tools.join(', ')}</td>
              </tr>
              <tr>
                <th>PII scrubbing</th>
                <td>
                  <Badge
                    value={policy.pii_scrubbing_enabled ? 'on' : 'off'}
                    kind={policy.pii_scrubbing_enabled ? 'approved' : 'rejected'}
                  />
                </td>
              </tr>
            </tbody>
          </table>
          <ul className="muted" style={{ marginTop: 14, paddingLeft: 18 }}>
            {policy.notes.map((note, i) => (
              <li key={i}>{note}</li>
            ))}
          </ul>
        </div>
      )}
    </>
  )
}
