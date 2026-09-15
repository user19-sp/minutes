# Architecture

C4-style layering, as specified in the blueprint. Person B owns everything here
except the ML services, which sit behind contracts Person A implements.

## Layers

```
┌──────────────────────────────────────────────────────────────────────┐
│ PRESENTATION   React reviewer editor (Vite)                          │
│                upload · review · correct · approve gates · export    │
├──────────────────────────────────────────────────────────────────────┤
│ API            FastAPI + Pydantic · JWT auth · RBAC                  │
│                35 endpoints · OpenAPI at /docs                       │
├──────────────────────────────────────────────────────────────────────┤
│ APPLICATION    Orchestrator · tool allow-list registry               │
│  (governance)  approval-gate state machine · audit writer            │
├──────────────────────────────────────────────────────────────────────┤
│ ML SERVICES    STT · diarization · agenda segmentation · extraction  │
│                Protocol-defined; baseline or Person A's models       │
├──────────────────────────────────────────────────────────────────────┤
│ DATA           PostgreSQL (SQLite in dev) · file storage             │
├──────────────────────────────────────────────────────────────────────┤
│ OPS            Docker · GitHub Actions · structlog · Prometheus      │
└──────────────────────────────────────────────────────────────────────┘
```

## The governed pipeline

```
  upload ─→ QUEUE ─→ worker claims ─→ [transcribe] ─→ [diarize?] ─→ scrub PII
                                                                      │
                                                                      ▼
                                                             store transcript
                                                              │
                                                              ▼
                                                      [segment agenda]
                                                              │
                                                              ▼
                                                  [extract decisions/actions]
                                                              │
                                                              ▼
                                            ╔═════════════════════════════╗
                                            ║   HUMAN GATE #1             ║
                                            ║   persist_minutes           ║
                                            ╚═════════════════════════════╝
                                                              │ approved
                                                              ▼
                                                    draft minutes stored
                                                              │
                                                              ▼
                                              reviewer accepts/corrects/rejects
                                                              │
                                                              ▼
                                            ╔═════════════════════════════╗
                                            ║   HUMAN GATE #2             ║
                                            ║   export_actions            ║
                                            ╚═════════════════════════════╝
                                                              │ approved
                                                              ▼
                                                     CSV / JSON / tracker
```

`[bracketed]` steps are allow-listed tool calls. Everything, including every
refusal, lands in the audit trail.

## The key design decision

**Tools that call ML services are pure reads. Exactly two tools can change the
world, and both are gated.**

Persistence of intermediate artefacts (transcript, agenda blocks) is done by the
orchestrator, not by the agent. This makes the security claim narrow and testable:

> The agent can alter stored state in exactly two ways, and a human authorises
> both.

| Tool | Effect | Gate | Calls/run |
|---|---|---|---|
| `transcribe_audio` | read | — | 2 |
| `diarize_speakers` | read | — | 2 |
| `segment_agenda` | read | — | 2 |
| `extract_decisions_actions` | read | — | 3 |
| `persist_minutes` | **write** | **required** | 1 |
| `export_actions` | **external** | **required** | 3 |

`ToolRegistry.register` raises if a `write`/`external` tool is registered without
`requires_approval`, so this table cannot silently drift.

## Suspension and resumption

A run is **not** a long-lived process. When a gated tool is reached:

1. the tool's *validated* arguments are written into the gate payload;
2. the payload is fingerprinted (SHA-256) and the fingerprint stored immutably;
3. the run returns with status `awaiting_approval`. Nothing is held in memory.

On approval, `resume_run` replays exactly the approved payload through the
allow-list. The approval therefore authorises **the specific action a human saw**,
not "whatever the agent decides next". If the stored payload no longer matches its
fingerprint, the grant is revoked and the tampering is audited.

This design also means moving runs behind a task queue is a transport change, not
a redesign — the state machine is already resumable.

## The ML seam (Person A)

The platform never imports torch, whisper or pyannote. It depends only on four
Protocols in `backend/app/ml/base.py`:

```python
SpeechToText.transcribe(audio_path, language_hint) -> TranscriptionResult
Diarizer.diarize(audio_path, segments)             -> DiarizationResult
AgendaSegmenter.segment(text, segments)            -> SegmentationResult
DecisionActionExtractor.extract(blocks)            -> ExtractionResult
```

Person A implements a class satisfying a Protocol, registers it in
`backend/app/ml/registry.py`, and it is selected by environment variable:

```bash
MOM_STT_BACKEND=whisper
MOM_EXTRACTOR_BACKEND=transformer
```

Backends are imported lazily, so `baseline` never imports torch. If a model's
dependencies are missing, the registry logs an error and falls back to the
baseline rather than taking the service down.

Consequences: the API, governance layer and UI are testable with no GPU and no
weights; swapping a model changes one binding; and both arms of the comparison
study use interchangeable implementations.

## Data model

```
users ──< jobs ──┬── transcripts ──< agenda_blocks
                 ├──< decisions ──< action_items
                 ├──< agent_runs ──< approval_requests
                 └──< exports

audit_events  (append-only; correlated by job_id / run_id / trace_id)
```

Notable columns:

| Column | Why it exists |
|---|---|
| `decisions.evidence_quote` | Verbatim transcript span — the anti-hallucination control |
| `decisions.original_text` | The model's proposal, kept when a human edits, for error analysis |
| `action_items.review_seconds` | Reviewer-correction-time metric, measured in the UI |
| `approval_requests.payload_fingerprint` | Binds a grant to exact arguments (T-03) |
| `agent_runs.denied_tool_call_count` | Refusals are evidence, so they are counted |
| `agent_runs.mode` | Separates the two arms of the comparison study |
| `agent_runs.claimed_by` / `claimed_at` | Queue lease: identifies the worker and detects abandoned runs |
| `transcripts.pii_redaction_count` | How much was removed before storage |

## Observability

Three correlated signals, joined by `trace_id`:

* **Logs** — structured JSON via structlog; secret-shaped keys stripped by a processor.
* **Metrics** — 22 Prometheus families at `/metrics`. Beyond the usual HTTP signals:
  `agent_tool_calls_total{tool,outcome}`, `approval_gates_total{action,status}`,
  `approval_wait_seconds`, `injection_attempts_total{rule,severity}`,
  `pii_redactions_total{kind}`, `reviewer_correction_seconds{item_type}`.
* **Traces** — `X-Trace-Id` on every request and response, stamped onto every audit row.

The `agent_*` and `reviewer_*` families are the quantitative half of the
comparison study.

## Deployment

```
        :8080                    :8000                  :5432
   ┌───────────┐  /api proxy  ┌─────────┐          ┌────────────┐
   │   nginx   │ ───────────→ │   api   │ ───────→ │  postgres  │
   │  (web)    │              │(uvicorn)│     ┌──→ │            │
   └───────────┘              └─────────┘     │    └────────────┘
     SPA static                non-root       │      not exposed
                             2 workers   ┌─────────┐   to host
                                         │ worker  │
                                         │ (queue) │
                                         └─────────┘
                                      claims and runs the pipeline
```

nginx reverse-proxies `/api`, so the browser is same-origin and the JWT never
travels cross-origin. The API container runs as a non-root user; Postgres is not
published to the host.

## Deliberate trade-offs

**Queued runs, with an inline mode for tests.** `POST /runs` enqueues and returns
202; a worker container claims the run and executes it. Real transcription takes
minutes and nginx closes the connection at 300s, so an inline run would fail on
exactly the realistic input a demo needs. Dev and tests keep `RUN_EXECUTION=inline`
so a test can assert on a finished run without polling — execution mode is
transport, and a test asserts both paths produce identical minutes.

The queue is the database, not Redis: `agent_runs` is already durable and already
carries mode, status and the audit correlation id, and a broker would add a
service and a failure mode for work measured in jobs per hour. Claiming uses
`SELECT … FOR UPDATE SKIP LOCKED` on PostgreSQL. A worker that dies mid-run has
its lease expire and the run requeued, up to a retry ceiling.

**SQLite in dev, Postgres in deployment.** Same SQLAlchemy models, switched by
`DATABASE_URL`. Lets the project run with no Docker installed. CI runs the full
suite against **both**, because enum handling, JSON columns and transaction
semantics differ.

**Regex PII detection.** Fast, explainable, no model dependency, runs in CI. It has
real false negatives — see R3.

**Vector database not yet used.** The blueprint lists Chroma/Qdrant for
embedding-based segmentation and retrieval-augmented extraction. Both sit on
Person A's side of the seam; the baseline segmenter is cue-phrase based and needs
no vector store. Adding one changes `EmbeddingAgendaSegmenter`, not the platform.

## Where things live

```
backend/app/
├── main.py              app wiring, middleware, /health, /metrics
├── config.py            settings from environment
├── models.py            ORM — the persistent data contract
├── schemas.py           Pydantic — the API contract
├── agent/               THE INNOVATION LAYER
│   ├── registry.py      tool allow-list + the five checks
│   ├── tools.py         the six tools, and nothing else
│   ├── gates.py         approval-gate state machine
│   ├── orchestrator.py  governed and control-arm pipelines
│   └── audit.py         append-only trail
├── ml/                  THE SEAM WITH PERSON A
│   ├── base.py          Protocols + result types
│   ├── baseline.py      rule-based implementations (also the study's baseline)
│   └── registry.py      backend binding by env var
├── security/            auth.py · pii.py · injection.py
├── services/            ingestion.py · export.py
├── observability/       logging.py · metrics.py · middleware.py
└── api/routes/          auth · jobs · runs · approvals · minutes · exports · audit
```
