# API contracts

Base path `/api/v1`. The live, authoritative spec is at `/docs` (Swagger) and
`/openapi.json` on a running server — this document explains the parts a schema
cannot: what the contract *means* and why it is shaped this way.

**Changing a field in `backend/app/schemas.py` is a contract change.** Update this
document alongside it.

## Conventions

| | |
|---|---|
| Auth | `Authorization: Bearer <jwt>` on everything except `/health`, `/metrics`, `/`, and the two `/auth` entry points |
| IDs | UUID4 strings, 36 characters |
| Timestamps | ISO 8601, UTC |
| Tracing | Every response carries `X-Trace-Id`; echo it when reporting a problem |
| Content type | `application/json`, except `POST /jobs` (multipart) and downloads |

### Error shape

```jsonc
// 4xx / 5xx
{ "detail": "Human-readable explanation.", "trace_id": "…" }

// 422 — validation
{
  "detail": "Request validation failed.",
  "errors": [{ "field": "body.mode", "message": "…", "type": "…" }],
  "trace_id": "…"
}
```

The submitted body is **never echoed back** — a reflected body is an XSS and
exfiltration gadget, and the client already knows what it sent. Stack traces are
never returned; the `trace_id` joins to the server log instead.

### Status codes with specific meaning

| Code | Meaning here |
|---|---|
| `202` | Export requested — **a gate was opened, nothing was written** |
| `404` | Not found **or** not yours. Deliberate: a 403 would confirm the id exists |
| `409` | State conflict — gate already decided, run already in progress, nothing to export |
| `413` | Upload exceeds `MAX_UPLOAD_MB` |
| `415` | Unsupported file extension or declared content type |

---

## Endpoints

### auth
| Method | Path | Summary |
|---|---|---|
| `POST` | `/auth/register` | Register a reviewer account |
| `POST` | `/auth/login` | Exchange credentials for a JWT |
| `GET` | `/auth/me` | Current user |
| `GET` | `/auth/users` | List accounts (admin only) |
| `GET` | `/auth/demo` | Whether one-click demo sign-in is available |
| `GET` | `/auth/registration-policy` | How registration is gated (public) |
| `GET` | `/auth/approved-emails` | The registration allow-list (admin only) |
| `POST` | `/auth/approved-emails` | Approve an address (admin only) |
| `DELETE` | `/auth/approved-emails/{id}` | Withdraw approval (admin only) |

Passwords are minimum 10 characters, maximum 72 bytes (bcrypt's limit — longer
input is **rejected**, not silently truncated). `role: "admin"` in a registration
request is downgraded to `reviewer`; only an existing admin can grant admin.

**Registration requires approval.** `POST /auth/register` returns **403** unless
the address is on the allow-list, which only an admin may edit. Withdrawing an
approval stops future registrations; it does not disable an account already
created with that address — deactivating a user is a separate, deliberate action.
`REGISTRATION_MODE=open` bypasses the list entirely.

`GET /auth/demo` tells the login screen whether to offer one-click sign-in. It
reports unavailable when the seeded account does not exist, so a fresh database
never advertises a login that would fail.

Login returns the same status and message for an unknown email and a wrong
password, and burns a dummy hash on the unknown-email path so response time does
not distinguish them.

### jobs
| Method | Path | Summary |
|---|---|---|
| `POST` | `/jobs` | Upload a recording or transcript (multipart) |
| `GET` | `/jobs` | List your meetings |
| `GET` | `/jobs/{job_id}` | Fetch one meeting |
| `GET` | `/jobs/{job_id}/transcript` | Fetch the PII-scrubbed transcript |
| `DELETE` | `/jobs/{job_id}` | Delete a meeting and its derived data |

`POST /jobs` fields: `file` (required), `title`, `language_hint`.

Two input kinds are accepted:

* **audio** — `.wav .mp3 .m4a .mp4 .flac .ogg .opus .webm .aac`, validated by
  magic bytes, up to `MAX_UPLOAD_MB`;
* **transcript** — `.txt .vtt .md`, validated as decodable UTF-8 with no NUL
  bytes, up to 5 MB. Skips the STT stage; everything downstream is identical.

The client filename is **metadata only**. Storage uses a generated UUID, which
closes path traversal and Windows device-name attacks.

`DELETE` removes the stored file and cascades the derived rows. **Audit events are
retained** — the trail records that a deletion happened, without retaining content.

### runs and governance
| Method | Path | Summary |
|---|---|---|
| `POST` | `/jobs/{job_id}/runs` | Start an orchestrator run |
| `GET` | `/jobs/{job_id}/runs` | Run history |
| `GET` | `/runs/{run_id}` | Fetch one run |
| `GET` | `/runs/{run_id}/trace` | Step-by-step trace |
| `POST` | `/runs/{run_id}/resume` | Resume a run suspended at a gate |
| `GET` | `/jobs/{job_id}/comparison` | Agent vs no-agent figures |
| `GET` | `/governance/tools` | The complete allow-list |
| `GET` | `/governance/policy` | Governance configuration in force |

```jsonc
// POST /jobs/{job_id}/runs
{ "mode": "agent",      // or "no_agent" — the ungoverned control arm
  "enable_diarization": false }
```

A successful governed run returns `status: "awaiting_approval"`, **not**
`"completed"`. That is the expected outcome: the run reached the write gate and
stopped. `denied_tool_call_count` records refusals — they are evidence, not errors.

`GET /governance/tools` returns each tool's name, side effect, whether it is gated,
its per-run call ceiling and its JSON input schema. It is served from the live
registry, so it cannot drift from what the code permits.

### approvals — the human-in-the-loop control point
| Method | Path | Summary |
|---|---|---|
| `GET` | `/approvals` | Approval queue (`?status=pending`) |
| `GET` | `/approvals/{approval_id}` | Inspect one gate |
| `POST` | `/approvals/{approval_id}/decide` | Approve or reject |
| `POST` | `/approvals/expire-stale` | Expire gates nobody decided |

```jsonc
// POST /approvals/{approval_id}/decide?resume=true
{ "decision": "approved",   // or "rejected"
  "note": "Checked items 2 and 4 against the recording." }
```

Contract guarantees:

1. Only a `pending` gate can be decided. Re-deciding returns **409** — an approval
   cannot be replayed to authorise a second write.
2. Only `reviewer` or `admin` may decide. Role is re-read from the database on
   every request, so a demotion takes effect immediately.
3. The approval is bound to a SHA-256 fingerprint of the exact `payload`,
   stamped when the gate opened. Approving *"CSV, approved items only"* does not
   authorise *"JSON, everything"*.
4. On approval the run resumes by default (`?resume=false` to defer), replaying
   exactly the approved payload.
5. Rejection aborts the run. **That is a success, not a failure** — the human said
   no and nothing was written.

The full `payload` is returned so a reviewer can inspect precisely what will run.
Approving a summary you cannot inspect would make the gate theatre.

### review
| Method | Path | Summary |
|---|---|---|
| `GET` | `/jobs/{job_id}/minutes` | Everything the editor needs, in one round trip |
| `POST` | `/decisions/{decision_id}/review` | Accept, correct or reject a decision |
| `POST` | `/actions/{action_id}/review` | Accept, correct or reject an action item |
| `GET` | `/jobs/{job_id}/review-stats` | Reviewer effort figures |

```jsonc
// POST /actions/{action_id}/review
{ "status": "edited",            // approved | rejected | edited
  "text": "Priya to send the API contract to the vendor.",
  "owner_name": "Priya",
  "deadline": "2026-09-19",
  "note": "Owner was wrong in the draft.",
  "review_seconds": 14.5 }
```

`text` is required when `status` is `"edited"`.

`review_seconds` is measured by the UI — the clock starts when the item renders
and is read at the moment of ruling. It feeds `reviewer_correction_seconds` and
the evaluation dossier, so it is measured rather than estimated later.

On first review the model's proposal is preserved in `original_text` (and
`original_owner_name`, `original_deadline`). Editing never destroys what the model
actually produced; error analysis depends on it.

`review-stats` returns `accept_rate` (share kept unchanged — a precision proxy),
`edit_rate`, and mean/total review seconds.

### export
| Method | Path | Summary |
|---|---|---|
| `POST` | `/jobs/{job_id}/exports` | Request an export — **opens a gate** |
| `GET` | `/jobs/{job_id}/exports` | Files produced |
| `GET` | `/exports/{export_id}/download` | Download one |

```jsonc
// POST → 202 Accepted. No file exists yet.
{ "status": "awaiting_approval",
  "run_id": "…",
  "approval": { "id": "…", "action": "export_actions", "risk": "high", … },
  "next_step": "POST /api/v1/approvals/{id}/decide" }
```

Export is the only path by which data leaves the system, so it is the strictest:

* `include_unapproved: false` (default) exports only reviewer-approved items. A
  **rejected item never exports**, even with the opt-in flag.
* Requesting an export with nothing eligible returns **409 before a gate is
  opened** — never ask a human to approve an action that cannot succeed; doing so
  trains reviewers to click through gates.
* CSV cells beginning `= + - @` are prefixed with `'` so they do not execute in
  Excel. Written UTF-8 with BOM so Devanagari renders correctly.
* Every file is SHA-256 hashed and recorded in `exports` for provenance.

Downloads require the bearer token, so a plain link will not work — fetch the
blob (the reviewer editor does this).

### audit
| Method | Path | Summary |
|---|---|---|
| `GET` | `/jobs/{job_id}/audit` | Trail for one meeting |
| `GET` | `/jobs/{job_id}/audit/export` | Download as JSONL |
| `GET` | `/runs/{run_id}/audit` | Trail for one run |
| `GET` | `/audit` | Global trail (admin only) |

**Read-only by construction.** There is deliberately no create, update or delete
endpoint: the API surface must not offer a way to edit history.

Action names are namespaced: `job.*`, `run.*`, `tool.*`, `gate.*`, `review.*`,
`export.*`, `auth.*`, `security.*`, `privacy.*`, `pipeline.*`.

The ones that carry the governance story:

| Action | Meaning |
|---|---|
| `tool.call` | An allow-listed call executed |
| `tool.denied` | A call was **refused** — with the reason |
| `tool.approval_required` | A gated tool was reached; the run suspended |
| `gate.opened` / `gate.approved` / `gate.rejected` | The human decision trail |
| `security.injection_detected` | Injection patterns found in untrusted content |
| `security.approval_payload_tampered` | A grant was revoked; the gated action did not run |
| `privacy.pii_redacted` | Identifiers removed before storage |
| `run.ungoverned_control_arm` | This run bypassed governance — study control arm |

### ops
| Method | Path | Summary |
|---|---|---|
| `GET` | `/health` | Liveness and readiness (includes a DB check) |
| `GET` | `/metrics` | Prometheus exposition — 22 families |
| `GET` | `/` | Service banner |

## Stability

Pre-1.0. The path prefix is versioned (`/api/v1`); a breaking change means `/v2`.
Within v1, fields may be **added** — clients must tolerate unknown fields — but
existing field names, types and enum values will not change meaning.
