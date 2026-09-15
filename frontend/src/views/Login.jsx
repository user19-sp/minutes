import { useState } from 'react'
import { api, setSession } from '../api'
import { Banner } from '../components/common'

export default function Login({ onSignedIn }) {
  const [mode, setMode] = useState('login')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [fullName, setFullName] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const submit = async (e) => {
    e.preventDefault()
    setError('')
    setBusy(true)
    try {
      if (mode === 'register') {
        await api.register({ email, password, full_name: fullName || null, role: 'reviewer' })
      }
      const session = await api.login(email, password)
      setSession(session.access_token, session.user)
      onSignedIn(session.user)
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="login-wrap">
      <div className="card">
        <h2>Meeting Intelligence</h2>
        <p className="hint">Reviewer editor — sign in to review and approve meeting minutes.</p>

        <Banner kind="error">{error}</Banner>

        <form onSubmit={submit} className="stack">
          {mode === 'register' && (
            <div>
              <label>Full name</label>
              <input value={fullName} onChange={(e) => setFullName(e.target.value)} />
            </div>
          )}
          <div>
            <label>Email</label>
            <input
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              required
              autoComplete="username"
            />
          </div>
          <div>
            <label>Password {mode === 'register' && '(at least 10 characters)'}</label>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
              minLength={mode === 'register' ? 10 : undefined}
              autoComplete={mode === 'register' ? 'new-password' : 'current-password'}
            />
          </div>
          <button className="primary" type="submit" disabled={busy} style={{ width: '100%' }}>
            {busy ? 'Please wait…' : mode === 'login' ? 'Sign in' : 'Create account & sign in'}
          </button>
        </form>

        <p className="muted" style={{ marginTop: 14, marginBottom: 0 }}>
          {mode === 'login' ? 'No account yet? ' : 'Already registered? '}
          <button
            className="small"
            onClick={() => {
              setMode(mode === 'login' ? 'register' : 'login')
              setError('')
            }}
          >
            {mode === 'login' ? 'Register' : 'Sign in'}
          </button>
        </p>
      </div>
    </div>
  )
}
