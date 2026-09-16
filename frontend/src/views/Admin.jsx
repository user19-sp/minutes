import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'
import { Banner, Empty, Spinner, formatTime } from '../components/common'

/**
 * Registration allow-list.
 *
 * This screen is the control made visible: an administrator names the addresses
 * that may create an account, and everything else is refused. The threat model
 * assumes no public registration — this is where that stops being an assumption.
 */
export default function Admin() {
  const [entries, setEntries] = useState([])
  const [policy, setPolicy] = useState(null)
  const [email, setEmail] = useState('')
  const [note, setNote] = useState('')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(false)
  const [loading, setLoading] = useState(true)

  const load = useCallback(async () => {
    try {
      const [list, pol] = await Promise.all([api.listApprovedEmails(), api.registrationPolicy()])
      setEntries(list)
      setPolicy(pol)
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  const add = async (e) => {
    e.preventDefault()
    setError('')
    setBusy(true)
    try {
      await api.approveEmail(email.trim(), note.trim() || null)
      setNotice(`${email.trim()} can now register.`)
      setEmail('')
      setNote('')
      await load()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  const revoke = async (entry) => {
    if (!confirm(`Stop ${entry.email} from registering?\n\nAny account already created with it keeps working.`)) return
    setBusy(true)
    try {
      const res = await api.revokeEmail(entry.id)
      setNotice(res.detail)
      await load()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  if (loading) return <Spinner label="Loading the allow-list…" />

  return (
    <>
      <Banner kind="error" onDismiss={() => setError('')}>{error}</Banner>
      <Banner kind="ok" onDismiss={() => setNotice('')}>{notice}</Banner>

      {policy && (
        <Banner kind={policy.self_service ? 'warn' : 'info'}>
          {policy.message}
          {policy.self_service && ' Set REGISTRATION_MODE=approved_only to restrict it.'}
        </Banner>
      )}

      <div className="card">
        <h2>Approve an address</h2>
        <p className="hint">
          Only addresses on this list can create an account. Everyone else is refused at
          registration, whether or not they have the link.
        </p>

        <form onSubmit={add} className="stack">
          <div className="row" style={{ alignItems: 'flex-end' }}>
            <div style={{ flex: 2, minWidth: 220 }}>
              <label>Email address</label>
              <input
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="new.person@example.com"
                required
              />
            </div>
            <div style={{ flex: 2, minWidth: 180 }}>
              <label>Note (optional)</label>
              <input
                value={note}
                onChange={(e) => setNote(e.target.value)}
                placeholder="e.g. Finance, joining Monday"
              />
            </div>
            <button className="primary" type="submit" disabled={busy}>
              {busy ? 'Adding…' : 'Approve'}
            </button>
          </div>
        </form>
      </div>

      <div className="card">
        <h2>Approved addresses</h2>
        <p className="hint">{entries.length} on the list</p>

        {entries.length === 0 ? (
          <Empty>Nobody is approved yet. Nobody can register.</Empty>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Email</th>
                <th>Note</th>
                <th>Status</th>
                <th>Added</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {entries.map((entry) => (
                <tr key={entry.id}>
                  <td className="mono">{entry.email}</td>
                  <td className="muted">{entry.note || '—'}</td>
                  <td>
                    {entry.used_at ? (
                      <span className="badge approved">registered</span>
                    ) : (
                      <span className="badge pending">not yet used</span>
                    )}
                  </td>
                  <td className="muted">{formatTime(entry.added_at)}</td>
                  <td>
                    <button className="small danger" disabled={busy} onClick={() => revoke(entry)}>
                      Remove
                    </button>
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
