import { useEffect, useState } from 'react'
import { api, clearSession, getStoredUser, getToken } from './api'
import Admin from './views/Admin'
import Audit from './views/Audit'
import Governance from './views/Governance'
import Login from './views/Login'
import Meetings from './views/Meetings'
import { Badge } from './components/common'

const TABS = [
  ['meetings', 'Meetings', null],
  ['governance', 'Governance', null],
  ['audit', 'Audit trail', null],
  // Only an admin can manage the registration allow-list, so only an admin is
  // shown the tab. The API enforces it regardless of what the UI renders.
  ['admin', 'Admin', 'admin'],
]

export default function App() {
  const [user, setUser] = useState(getStoredUser())
  const [tab, setTab] = useState('meetings')
  const [checking, setChecking] = useState(Boolean(getToken()))

  // A token in localStorage proves nothing: verify it against the server before
  // rendering the app, so an expired session shows the login screen immediately
  // rather than a broken dashboard.
  useEffect(() => {
    if (!getToken()) { setChecking(false); return }
    api.me()
      .then(setUser)
      .catch(() => { clearSession(); setUser(null) })
      .finally(() => setChecking(false))
  }, [])

  useEffect(() => {
    const onUnauthorized = () => setUser(null)
    window.addEventListener('mom:unauthorized', onUnauthorized)
    return () => window.removeEventListener('mom:unauthorized', onUnauthorized)
  }, [])

  if (checking) return null
  if (!user) return <Login onSignedIn={setUser} />

  return (
    <div className="app">
      <header className="topbar">
        <h1>Meeting Intelligence</h1>
        <span className="muted">human-governed minutes</span>
        <nav>
          {TABS.filter(([, , role]) => !role || user.role === role).map(([key, label]) => (
            <button key={key} className={tab === key ? 'active' : ''} onClick={() => setTab(key)}>
              {label}
            </button>
          ))}
        </nav>
        <span className="muted">{user.email}</span>
        <Badge value={user.role} />
        <button className="small" onClick={() => { clearSession(); setUser(null) }}>
          Sign out
        </button>
      </header>

      <main className="container">
        {tab === 'meetings' && <Meetings />}
        {tab === 'governance' && <Governance />}
        {tab === 'audit' && <Audit />}
        {tab === 'admin' && user.role === 'admin' && <Admin />}
      </main>
    </div>
  )
}
