import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api'
import ApprovalGate from '../components/ApprovalGate'
import ReviewItem from '../components/ReviewItem'
import { Badge, Banner, Empty, RedactedText, Spinner, formatDuration, formatTime } from '../components/common'

export default function Meetings() {
  const [jobs, setJobs] = useState([])
  const [selected, setSelected] = useState(null)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [loading, setLoading] = useState(true)

  const refreshJobs = useCallback(async () => {
    try {
      setJobs(await api.listJobs())
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    refreshJobs()
  }, [refreshJobs])

  return (
    <>
      <Banner kind="error" onDismiss={() => setError('')}>
        {error}
      </Banner>
      <Banner kind="ok" onDismiss={() => setNotice('')}>
        {notice}
      </Banner>

      <div className="grid-2">
        <div>
          <UploadPanel
            onUploaded={(job) => {
              refreshJobs()
              setSelected(job.id)
              setNotice(`Uploaded “${job.title || job.original_filename}”. Start a run to process it.`)
            }}
            onError={setError}
          />
          <div className="card">
            <h2>Meetings</h2>
            <p className="hint">{jobs.length} uploaded</p>
            {loading ? (
              <Spinner label="Loading meetings…" />
            ) : jobs.length === 0 ? (
              <Empty>No meetings yet. Upload a recording to begin.</Empty>
            ) : (
              <ul className="list">
                {jobs.map((job) => (
                  <li
                    key={job.id}
                    className={`clickable ${selected === job.id ? 'selected' : ''}`}
                    onClick={() => setSelected(job.id)}
                  >
                    <div className="row">
                      <strong style={{ flex: 1 }}>{job.title || job.original_filename}</strong>
                      <Badge value={job.status} />
                    </div>
                    <div className="muted">{formatTime(job.created_at)}</div>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>

        <div>
          {selected ? (
            <MeetingDetail
              jobId={selected}
              onError={setError}
              onNotice={setNotice}
              onJobChanged={refreshJobs}
              onDeleted={() => {
                setSelected(null)
                refreshJobs()
              }}
            />
          ) : (
            <div className="card">
              <Empty>Select a meeting to review its minutes.</Empty>
            </div>
          )}
        </div>
      </div>
    </>
  )
}

/* ------------------------------------------------------------------ upload */

function UploadPanel({ onUploaded, onError }) {
  const [title, setTitle] = useState('')
  const [languageHint, setLanguageHint] = useState('')
  const [busy, setBusy] = useState(false)
  const fileRef = useRef(null)

  const submit = async (e) => {
    e.preventDefault()
    const file = fileRef.current?.files?.[0]
    if (!file) return
    setBusy(true)
    try {
      const job = await api.uploadJob(file, title, languageHint)
      setTitle('')
      if (fileRef.current) fileRef.current.value = ''
      onUploaded(job)
    } catch (err) {
      onError(err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="card">
      <h2>Upload a meeting</h2>
      <p className="hint">
        An audio recording, or a <strong>.txt transcript</strong> if the meeting is already
        written up. Files are validated by their actual content, not just the extension.
      </p>
      <form onSubmit={submit} className="stack">
        <div>
          <label>Meeting title</label>
          <input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Sprint review" />
        </div>
        <div>
          <label>Primary language (optional hint)</label>
          <select value={languageHint} onChange={(e) => setLanguageHint(e.target.value)}>
            <option value="">Auto-detect</option>
            <option value="en">English</option>
            <option value="hi">Hindi</option>
            <option value="mr">Marathi</option>
            <option value="ta">Tamil</option>
          </select>
        </div>
        <div>
          <label>Audio recording or transcript</label>
          <input
            type="file"
            ref={fileRef}
            accept="audio/*,.wav,.mp3,.m4a,.ogg,.flac,.webm,.txt,.vtt,.md"
            required
          />
        </div>
        <button className="primary" type="submit" disabled={busy}>
          {busy ? 'Uploading…' : 'Upload'}
        </button>
      </form>
    </div>
  )
}

/* ------------------------------------------------------------------ detail */

function MeetingDetail({ jobId, onError, onNotice, onJobChanged, onDeleted }) {
  const [minutes, setMinutes] = useState(null)
  const [trace, setTrace] = useState(null)
  const [stats, setStats] = useState(null)
  const [exports, setExports] = useState([])
  const [busy, setBusy] = useState(false)
  const [tab, setTab] = useState('minutes')
  const [diarize, setDiarize] = useState(true)

  const load = useCallback(async () => {
    try {
      const data = await api.getMinutes(jobId)
      setMinutes(data)
      setExports(await api.listExports(jobId))
      setStats(await api.reviewStats(jobId))
      if (data.latest_run) setTrace(await api.getTrace(data.latest_run.id))
      else setTrace(null)
    } catch (err) {
      onError(err.message)
    }
  }, [jobId, onError])

  useEffect(() => {
    setMinutes(null)
    load()
  }, [jobId, load])

  // While a run is queued or executing there is nothing to react to, so poll.
  // Cheap (one request every 2s) and it stops the moment the job settles, so an
  // idle meeting costs nothing.
  const inFlight = minutes && ['queued', 'running'].includes(minutes.job.status)
  useEffect(() => {
    if (!inFlight) return undefined
    const timer = setInterval(load, 2000)
    return () => clearInterval(timer)
  }, [inFlight, load])

  const guard = async (fn, successMessage) => {
    setBusy(true)
    try {
      await fn()
      await load()
      onJobChanged()
      if (successMessage) onNotice(successMessage)
    } catch (err) {
      onError(err.message)
    } finally {
      setBusy(false)
    }
  }

  if (!minutes) {
    return (
      <div className="card">
        <Spinner label="Loading minutes…" />
      </div>
    )
  }

  const { job, transcript, agenda_blocks, decisions, action_items, pending_approvals, latest_run } = minutes
  const canRun = !['queued', 'running', 'awaiting_approval'].includes(job.status)

  return (
    <>
      <div className="card">
        <div className="row">
          <h2 style={{ flex: 1, marginBottom: 0 }}>{job.title || job.original_filename}</h2>
          <Badge value={job.status} />
        </div>
        <p className="hint" style={{ marginTop: 6 }}>
          {(job.size_bytes / 1024).toFixed(0)} KB · uploaded {formatTime(job.created_at)} ·{' '}
          <span className="mono">sha256:{job.sha256.slice(0, 12)}…</span>
        </p>

        {job.error_message && <Banner kind="error">{job.error_message}</Banner>}

        {job.status === 'queued' && (
          <Banner kind="info">
            <Spinner label="Queued — waiting for a worker to pick this up." />
          </Banner>
        )}
        {job.status === 'running' && (
          <Banner kind="info">
            <Spinner label="Transcribing and extracting. Long recordings take a few minutes." />
          </Banner>
        )}

        <div className="row">
          <label style={{ margin: 0, display: 'flex', alignItems: 'center', gap: 6 }}>
            <input
              type="checkbox"
              style={{ width: 'auto' }}
              checked={diarize}
              onChange={(e) => setDiarize(e.target.checked)}
            />
            Identify speakers
          </label>
          <button
            className="primary"
            disabled={busy || !canRun}
            onClick={() =>
              guard(
                () => api.startRun(job.id, 'agent', diarize),
                'Pipeline started. This page updates as it progresses.'
              )
            }
          >
            {busy ? 'Starting…' : latest_run ? 'Re-run pipeline' : 'Run pipeline'}
          </button>
          <button
            disabled={busy || !canRun}
            title="Control arm for the comparison study: no allow-list, no approval gate"
            onClick={() =>
              guard(
                () => api.startRun(job.id, 'no_agent', diarize),
                'Control-arm run finished (ungoverned).'
              )
            }
          >
            Run control arm
          </button>
          <button
            className="danger"
            disabled={busy}
            onClick={() => {
              if (confirm('Delete this meeting, its recording and all derived data?')) {
                guard(() => api.deleteJob(job.id).then(onDeleted), 'Meeting deleted.')
              }
            }}
          >
            Delete
          </button>
        </div>
      </div>

      {pending_approvals.map((gate) => (
        <ApprovalGate
          key={gate.id}
          gate={gate}
          busy={busy}
          onDecide={(id, decision, note) =>
            guard(
              () => api.decideApproval(id, decision, note),
              decision === 'approved' ? 'Approved — the action has run.' : 'Rejected — nothing was written.'
            )
          }
        />
      ))}

      <div className="card">
        <div className="row" style={{ marginBottom: 14 }}>
          {['minutes', 'transcript', 'trace', 'export'].map((t) => (
            <button key={t} className={tab === t ? 'primary small' : 'small'} onClick={() => setTab(t)}>
              {t[0].toUpperCase() + t.slice(1)}
            </button>
          ))}
        </div>

        {tab === 'minutes' && (
          <MinutesTab
            decisions={decisions}
            actions={action_items}
            stats={stats}
            busy={busy}
            onReviewDecision={(id, payload) => guard(() => api.reviewDecision(id, payload))}
            onReviewAction={(id, payload) => guard(() => api.reviewAction(id, payload))}
          />
        )}

        {tab === 'transcript' && <TranscriptTab transcript={transcript} blocks={agenda_blocks} />}

        {tab === 'trace' && <TraceTab trace={trace} run={latest_run} />}

        {tab === 'export' && (
          <ExportTab
            job={job}
            exports={exports}
            actions={action_items}
            busy={busy}
            onExport={(format) =>
              guard(
                () => api.requestExport(job.id, format, false),
                'Export requested — approve the gate above to produce the file.'
              )
            }
          />
        )}
      </div>
    </>
  )
}

/* -------------------------------------------------------------------- tabs */

function MinutesTab({ decisions, actions, stats, busy, onReviewDecision, onReviewAction }) {
  if (decisions.length === 0 && actions.length === 0) {
    return <Empty>No minutes yet. Run the pipeline, then approve the write gate.</Empty>
  }
  return (
    <>
      {stats && (
        <p className="muted" style={{ marginTop: 0 }}>
          Reviewed {stats.action_items.reviewed}/{stats.action_items.total} actions ·{' '}
          {stats.action_items.accept_rate != null
            ? `${Math.round(stats.action_items.accept_rate * 100)}% accepted unchanged`
            : 'no reviews yet'}
          {stats.action_items.mean_review_seconds != null &&
            ` · ${stats.action_items.mean_review_seconds}s mean review time`}
        </p>
      )}

      <h3>Decisions ({decisions.length})</h3>
      {decisions.length === 0 ? (
        <Empty>No decisions were extracted from this meeting.</Empty>
      ) : (
        decisions.map((d) => (
          <ReviewItem key={d.id} item={d} type="decision" busy={busy} onReview={onReviewDecision} />
        ))
      )}

      <h3 style={{ marginTop: 22 }}>Action items ({actions.length})</h3>
      {actions.length === 0 ? (
        <Empty>No action items were extracted from this meeting.</Empty>
      ) : (
        actions.map((a) => (
          <ReviewItem key={a.id} item={a} type="action" busy={busy} onReview={onReviewAction} />
        ))
      )}
    </>
  )
}

function TranscriptTab({ transcript, blocks }) {
  if (!transcript) return <Empty>No transcript yet.</Empty>
  return (
    <>
      <p className="muted" style={{ marginTop: 0 }}>
        {transcript.model_name} · languages: {transcript.detected_languages.join(', ') || 'unknown'}
        {transcript.is_code_mixed && ' · code-mixed'} · {transcript.pii_redaction_count} identifier(s)
        redacted before storage
      </p>

      <h3>Agenda blocks ({blocks.length})</h3>
      <ul className="list">
        {blocks.map((b) => (
          <li key={b.id}>
            <strong>
              {b.position + 1}. {b.title}
            </strong>
            <div className="muted">
              {b.text.slice(0, 180)}
              {b.text.length > 180 && '…'}
            </div>
          </li>
        ))}
      </ul>

      <h3 style={{ marginTop: 20 }}>Full transcript</h3>
      <div className="transcript">
        {transcript.segments.length > 0 ? (
          transcript.segments.map((s, i) => (
            <div key={i} style={{ marginBottom: 6 }}>
              <span className="mono" style={{ color: 'var(--muted)' }}>
                [{s.start.toFixed(1)}s]{s.speaker ? ` ${s.speaker}` : ''}
              </span>{' '}
              <RedactedText text={s.text} />
            </div>
          ))
        ) : (
          <RedactedText text={transcript.text} />
        )}
      </div>
    </>
  )
}

function TraceTab({ trace, run }) {
  if (!trace || !run) return <Empty>No run yet.</Empty>
  return (
    <>
      <p className="muted" style={{ marginTop: 0 }}>
        Mode <strong>{run.mode}</strong> · status <strong>{run.status}</strong> ·{' '}
        {trace.tool_calls} tool call(s), {trace.denied_tool_calls} denied ·{' '}
        {formatDuration(run.duration_ms)} · <span className="mono">trace {run.trace_id.slice(0, 8)}</span>
      </p>

      {trace.steps.length === 0 ? (
        <Empty>This run recorded no governed tool calls.</Empty>
      ) : (
        trace.steps.map((step, i) => (
          <div key={i} className={`trace-step ${step.outcome || ''}`}>
            <span className="dot" />
            <span className="mono" style={{ flex: 1 }}>
              {step.tool || step.stage}
            </span>
            <span className="muted">
              {step.outcome === 'denied' ? `DENIED — ${step.reason}` : step.outcome || 'ungoverned'}
            </span>
            <span className="muted">{step.duration_ms ? `${step.duration_ms} ms` : ''}</span>
          </div>
        ))
      )}
    </>
  )
}

function ExportTab({ job, exports, actions, busy, onExport }) {
  const ready = actions.filter((a) => ['approved', 'edited'].includes(a.status)).length

  return (
    <>
      <p className="muted" style={{ marginTop: 0 }}>
        {ready} action item(s) are reviewer-approved and eligible for export. Un-reviewed and
        rejected items are never exported.
      </p>

      <div className="row" style={{ marginBottom: 18 }}>
        {['csv', 'json', 'tracker'].map((fmt) => (
          <button key={fmt} disabled={busy || ready === 0} onClick={() => onExport(fmt)}>
            Request {fmt.toUpperCase()} export
          </button>
        ))}
      </div>

      {ready === 0 && (
        <Banner kind="warn">Approve at least one action item before requesting an export.</Banner>
      )}

      <h3>Files produced</h3>
      {exports.length === 0 ? (
        <Empty>Nothing has been exported from this meeting.</Empty>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Format</th>
              <th>Items</th>
              <th>Checksum</th>
              <th>Created</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {exports.map((e) => (
              <tr key={e.id}>
                <td>{e.format.toUpperCase()}</td>
                <td>{e.item_count}</td>
                <td className="mono">{e.sha256.slice(0, 12)}…</td>
                <td>{formatTime(e.created_at)}</td>
                <td>
                  <a href={`/api/v1/exports/${e.id}/download`} onClick={(ev) => {
                    // The download needs the bearer token, so fetch it and hand the
                    // browser a blob rather than navigating (which sends no header).
                    ev.preventDefault()
                    api.downloadExport(e.id).then(async (res) => {
                      const blob = await res.blob()
                      const url = URL.createObjectURL(blob)
                      const a = document.createElement('a')
                      a.href = url
                      a.download = `${job.id.slice(0, 8)}_actions.${e.format === 'csv' ? 'csv' : 'json'}`
                      a.click()
                      URL.revokeObjectURL(url)
                    })
                  }}>
                    Download
                  </a>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  )
}
