/**
 * Thin API client.
 *
 * The JWT lives in localStorage and is attached to every request. Every response
 * carries an X-Trace-Id header; we surface it on errors so a reviewer can quote
 * it when reporting a problem, and it joins straight to the backend's logs and
 * audit rows.
 */

const TOKEN_KEY = 'mom.token'
const USER_KEY = 'mom.user'
const BASE = '/api/v1'

export function getToken() {
  return localStorage.getItem(TOKEN_KEY)
}

export function getStoredUser() {
  const raw = localStorage.getItem(USER_KEY)
  return raw ? JSON.parse(raw) : null
}

export function setSession(token, user) {
  localStorage.setItem(TOKEN_KEY, token)
  localStorage.setItem(USER_KEY, JSON.stringify(user))
}

export function clearSession() {
  localStorage.removeItem(TOKEN_KEY)
  localStorage.removeItem(USER_KEY)
}

export class ApiError extends Error {
  constructor(message, status, traceId, body) {
    super(message)
    this.status = status
    this.traceId = traceId
    this.body = body
  }
}

async function request(path, { method = 'GET', body, isForm = false, raw = false } = {}) {
  const headers = {}
  const token = getToken()
  if (token) headers.Authorization = `Bearer ${token}`
  if (body && !isForm) headers['Content-Type'] = 'application/json'

  const res = await fetch(`${BASE}${path}`, {
    method,
    headers,
    body: isForm ? body : body ? JSON.stringify(body) : undefined,
  })

  const traceId = res.headers.get('X-Trace-Id')

  if (res.status === 401) {
    clearSession()
    window.dispatchEvent(new CustomEvent('mom:unauthorized'))
    throw new ApiError('Your session has expired. Please sign in again.', 401, traceId)
  }

  if (raw) {
    if (!res.ok) throw new ApiError('Download failed.', res.status, traceId)
    return res
  }

  const text = await res.text()
  const payload = text ? JSON.parse(text) : null

  if (!res.ok) {
    const detail =
      payload?.detail ||
      payload?.errors?.map((e) => `${e.field}: ${e.message}`).join('; ') ||
      `Request failed (${res.status})`
    throw new ApiError(detail, res.status, traceId, payload)
  }
  return payload
}

export const api = {
  // --- auth ---
  login: (email, password) => request('/auth/login', { method: 'POST', body: { email, password } }),
  register: (payload) => request('/auth/register', { method: 'POST', body: payload }),
  me: () => request('/auth/me'),
  demoAvailability: () => request('/auth/demo'),
  registrationPolicy: () => request('/auth/registration-policy'),
  listApprovedEmails: () => request('/auth/approved-emails'),
  approveEmail: (email, note) =>
    request('/auth/approved-emails', { method: 'POST', body: { email, note } }),
  revokeEmail: (id) => request(`/auth/approved-emails/${id}`, { method: 'DELETE' }),

  // --- jobs ---
  listJobs: () => request('/jobs'),
  getJob: (id) => request(`/jobs/${id}`),
  deleteJob: (id) => request(`/jobs/${id}`, { method: 'DELETE' }),
  uploadJob: (file, title, languageHint) => {
    const form = new FormData()
    form.append('file', file)
    if (title) form.append('title', title)
    if (languageHint) form.append('language_hint', languageHint)
    return request('/jobs', { method: 'POST', body: form, isForm: true })
  },

  // --- pipeline ---
  startRun: (jobId, mode, enableDiarization) =>
    request(`/jobs/${jobId}/runs`, {
      method: 'POST',
      body: { mode, enable_diarization: enableDiarization },
    }),
  listRuns: (jobId) => request(`/jobs/${jobId}/runs`),
  getTrace: (runId) => request(`/runs/${runId}/trace`),
  getComparison: (jobId) => request(`/jobs/${jobId}/comparison`),

  // --- review ---
  getMinutes: (jobId) => request(`/jobs/${jobId}/minutes`),
  reviewDecision: (id, payload) =>
    request(`/decisions/${id}/review`, { method: 'POST', body: payload }),
  reviewAction: (id, payload) => request(`/actions/${id}/review`, { method: 'POST', body: payload }),
  reviewStats: (jobId) => request(`/jobs/${jobId}/review-stats`),

  // --- governance ---
  listApprovals: (status = 'pending') => request(`/approvals?status=${status}`),
  decideApproval: (id, decision, note) =>
    request(`/approvals/${id}/decide`, { method: 'POST', body: { decision, note } }),
  listTools: () => request('/governance/tools'),
  getPolicy: () => request('/governance/policy'),

  // --- export & audit ---
  requestExport: (jobId, format, includeUnapproved) =>
    request(`/jobs/${jobId}/exports`, {
      method: 'POST',
      body: { format, include_unapproved: includeUnapproved },
    }),
  listExports: (jobId) => request(`/jobs/${jobId}/exports`),
  downloadExport: (id) => request(`/exports/${id}/download`, { raw: true }),
  getAudit: (jobId) => request(`/jobs/${jobId}/audit?limit=500`),
}
