"""The agent/copilot orchestrator -- the innovation layer.

It drives the pipeline as a sequence of allow-listed tool calls, suspends at every
human gate, and leaves a complete trace. Two modes run the same ML work so the
comparison study is like-for-like:

  AGENT mode     every step goes through the allow-list; the write and the export
                 stop at a human gate; every call, denial and ruling is audited.

  NO_AGENT mode  the control arm. Same models, called directly: no allow-list, no
                 gate, no per-tool trace, straight to the database. This is what
                 the project looks like *without* the governance layer, and it is
                 what the comparison measures against. It is never the default and
                 the API marks it `unsafe_control_arm`.

Suspension and resumption
-------------------------
A run is not a long-lived process. When a gated tool is reached, the orchestrator
writes the tool's validated arguments into the gate payload and returns with
status AWAITING_APPROVAL. Nothing is held in memory. When a human approves, the
API calls `resume_run`, which replays exactly the approved payload through the
allow-list. So an approval authorises the specific action a human saw -- not
"whatever the agent decides to do next".
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.agent import gates
from backend.app.agent.audit import AuditLogger
from backend.app.agent.registry import (
    ApprovalRequired,
    ToolContext,
    ToolDenied,
    ToolExecutionError,
    registry,
)
from backend.app.agent.tools import AGENT_TOOLS, PIPELINE_TOOLS
from backend.app.config import settings
from backend.app.ml import registry as ml
from backend.app.models import (
    AgendaBlock,
    AgentRun,
    ApprovalStatus,
    ItemStatus,
    Job,
    JobStatus,
    RunMode,
    RunStatus,
    Transcript,
)
from backend.app.observability.logging import get_logger, run_id_ctx
from backend.app.observability.metrics import (
    agent_run_duration_seconds,
    agent_runs_total,
    injection_attempts_total,
    pii_redactions_total,
    pipeline_stage_duration_seconds,
    queue_wait_seconds,
)
from backend.app.security import pii
from backend.app.security.injection import scan

log = get_logger("agent.orchestrator")


class OrchestratorError(Exception):
    """The run could not proceed."""


def _now() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------- #
# Run lifecycle
# --------------------------------------------------------------------------- #


def start_run(
    db: Session,
    audit: AuditLogger,
    *,
    job: Job,
    actor_id: str,
    mode: RunMode = RunMode.AGENT,
    enable_diarization: bool = False,
) -> AgentRun:
    """Create a run and drive it to completion in the caller's thread.

    The inline path: used by tests, by the dev default, and by anything that
    wants the result in hand when the call returns. The queued path creates the
    run separately and a worker calls `execute_run` on it instead.
    """
    run = AgentRun(
        job_id=job.id,
        mode=mode,
        status=RunStatus.RUNNING,
        enable_diarization=enable_diarization,
    )
    db.add(run)
    db.flush()

    audit.human(
        "run.started",
        user_id=actor_id,
        job_id=job.id,
        run_id=run.id,
        resource_type="agent_run",
        resource_id=run.id,
        detail={
            "mode": mode.value,
            "enable_diarization": enable_diarization,
            "backends": ml.active_backends(),
            "allowed_tools": sorted(AGENT_TOOLS if mode is RunMode.AGENT else PIPELINE_TOOLS),
        },
    )
    return execute_run(db, audit, job=job, run=run)


def execute_run(db: Session, audit: AuditLogger, *, job: Job, run: AgentRun) -> AgentRun:
    """Drive an existing run until it completes, suspends at a gate, or fails.

    Split out from `start_run` so a worker can execute a run it claimed from the
    queue: the row already exists and is already marked RUNNING, so re-creating
    it would duplicate the work and open two gates for one meeting.
    """
    token = run_id_ctx.set(run.id)
    started = time.perf_counter()

    # Record how long the reviewer waited before anything actually began.
    queued_at = run.queued_at
    if queued_at is not None:
        waited = (
            datetime.now(UTC) - (queued_at if queued_at.tzinfo else queued_at.replace(tzinfo=UTC))
        ).total_seconds()
        queue_wait_seconds.labels(mode=run.mode.value).observe(max(0.0, waited))

    run.status = RunStatus.RUNNING
    job.status = JobStatus.RUNNING
    db.flush()

    try:
        if run.mode is RunMode.NO_AGENT:
            _run_ungoverned(db, audit, job, run, run.enable_diarization)
        else:
            _run_governed(db, audit, job, run, run.enable_diarization)
    except Exception as exc:
        _fail(db, audit, job, run, exc)
    finally:
        _finalise(db, run, started)
        run_id_ctx.reset(token)

    return run


def resume_run(db: Session, audit: AuditLogger, *, run: AgentRun, actor_id: str) -> AgentRun:
    """Continue a suspended run by replaying its approved gate payloads."""
    if run.status is not RunStatus.AWAITING_APPROVAL:
        raise OrchestratorError(
            f"Run {run.id} is {run.status.value}; only a suspended run can be resumed."
        )

    job = db.get(Job, run.job_id)
    if job is None:
        raise OrchestratorError(f"Job {run.job_id} no longer exists.")

    token = run_id_ctx.set(run.id)
    started = time.perf_counter()
    run.status = RunStatus.RUNNING

    audit.human(
        "run.resumed",
        user_id=actor_id,
        job_id=job.id,
        run_id=run.id,
        resource_type="agent_run",
        resource_id=run.id,
    )

    try:
        ctx = _context(db, audit, job, run)
        approved = (
            db.execute(
                select(gates.ApprovalRequest)
                .where(
                    gates.ApprovalRequest.run_id == run.id,
                    gates.ApprovalRequest.status == ApprovalStatus.APPROVED,
                )
                .order_by(gates.ApprovalRequest.decided_at)
            )
            .scalars()
            .all()
        )

        if not approved:
            # Every gate was rejected or expired: the human said no. That is a
            # successful governance outcome, not an error.
            run.status = RunStatus.ABORTED
            job.status = JobStatus.READY_FOR_REVIEW
            audit.agent(
                "run.aborted_no_approval",
                run_id=run.id,
                job_id=job.id,
                detail={"reason": "no approved gates"},
            )
        else:
            prior_trace = list(run.plan or [])
            for gate in approved:
                if gate.action not in registry.names():
                    continue
                _invoke(ctx, gate.action, gate.payload)
            run.plan = prior_trace + list(ctx.trace)
            run.tool_call_count = (run.tool_call_count or 0) + ctx.total_calls
            run.denied_tool_call_count = (run.denied_tool_call_count or 0) + ctx.denied_calls
            _mark_ready(db, job, run)
    except Exception as exc:
        _fail(db, audit, job, run, exc)
    finally:
        _finalise(db, run, started)
        run_id_ctx.reset(token)

    return run


# --------------------------------------------------------------------------- #
# Governed pipeline (AGENT mode)
# --------------------------------------------------------------------------- #


def _context(db: Session, audit: AuditLogger, job: Job, run: AgentRun) -> ToolContext:
    allowed = AGENT_TOOLS if run.mode is RunMode.AGENT else PIPELINE_TOOLS
    return ToolContext(
        run_id=run.id,
        job_id=job.id,
        db=db,
        audit=audit,
        allowed_tools=set(allowed),
        max_total_calls=settings.agent_max_tool_calls,
        granted_approvals=gates.granted_keys_for_run(db, run.id, audit),
    )


def _invoke(ctx: ToolContext, tool: str, arguments: dict[str, Any]) -> Any:
    """Single funnel for every tool call the orchestrator makes."""
    stage_start = time.perf_counter()
    try:
        return registry.invoke(tool, arguments, ctx)
    finally:
        pipeline_stage_duration_seconds.labels(stage=tool).observe(
            time.perf_counter() - stage_start
        )


def _sync_counters(run: AgentRun, ctx: ToolContext) -> None:
    """Copy per-run governance counters onto the row.

    `plan` is reassigned from a copy rather than mutated: SQLAlchemy's JSON column
    tracks reassignment, not in-place list mutation, so mutating ctx.trace alone
    would silently fail to persist the trace.
    """
    run.plan = list(ctx.trace)
    run.tool_call_count = ctx.total_calls
    run.denied_tool_call_count = ctx.denied_calls


def _run_governed(
    db: Session,
    audit: AuditLogger,
    job: Job,
    run: AgentRun,
    enable_diarization: bool,
) -> None:
    ctx = _context(db, audit, job, run)

    # --- 1. transcribe ---------------------------------------------------- #
    stt = _invoke(ctx, "transcribe_audio", {"job_id": job.id, "language_hint": job.language_hint})
    segments = stt["segments"]

    # --- 2. diarize (optional module) ------------------------------------- #
    speaker_count = 0
    if enable_diarization:
        try:
            diarized = _invoke(ctx, "diarize_speakers", {"job_id": job.id, "segments": segments})
            segments = diarized["segments"]
            speaker_count = diarized["speaker_count"]
        except (ToolDenied, ToolExecutionError) as exc:
            # Diarization is explicitly optional; degrade rather than fail the run.
            audit.agent(
                "stage.skipped",
                run_id=run.id,
                job_id=job.id,
                outcome="degraded",
                detail={"stage": "diarize_speakers", "error": str(exc)[:300]},
            )

    # --- scrub + scan before anything is stored --------------------------- #
    transcript = _persist_transcript(
        db, audit, job, run, stt, segments, enable_diarization, speaker_count
    )

    # --- 3. agenda segmentation ------------------------------------------- #
    segmented = _invoke(ctx, "segment_agenda", {"job_id": job.id, "text": transcript.text})
    _persist_blocks(db, transcript, segmented["blocks"])

    # --- 4. extraction ----------------------------------------------------- #
    extracted = _invoke(
        ctx, "extract_decisions_actions", {"job_id": job.id, "blocks": segmented["blocks"]}
    )

    # --- 5. persist minutes (GATED) ---------------------------------------- #
    persist_args = {
        "job_id": job.id,
        "source_tool": extracted.get("model_name", "unknown"),
        "decisions": extracted["decisions"],
        "actions": extracted["actions"],
    }
    _sync_counters(run, ctx)

    try:
        _invoke(ctx, "persist_minutes", persist_args)
    except ApprovalRequired as required:
        _suspend(db, audit, job, run, ctx, required, persist_args)
        return

    _sync_counters(run, ctx)
    _mark_ready(db, job, run)


def _suspend(
    db: Session,
    audit: AuditLogger,
    job: Job,
    run: AgentRun,
    ctx: ToolContext,
    required: ApprovalRequired,
    arguments: dict[str, Any],
) -> None:
    """Park the run at a human gate. The validated arguments become the gate payload
    so the reviewer approves exactly what will run."""
    n_dec = len(arguments.get("decisions", []))
    n_act = len(arguments.get("actions", []))
    gates.open_gate(
        db,
        audit,
        job_id=job.id,
        run_id=run.id,
        action=required.tool,
        summary=(
            f"Write {n_dec} decision(s) and {n_act} action item(s) to the record for "
            f"'{job.title or job.original_filename}'. Nothing is stored until you approve."
        ),
        payload=required.payload,
        risk=required.risk,
    )
    run.status = RunStatus.AWAITING_APPROVAL
    _sync_counters(run, ctx)
    job.status = JobStatus.AWAITING_APPROVAL
    db.flush()
    log.info("run_suspended", run_id=run.id, tool=required.tool, job_id=job.id)


# --------------------------------------------------------------------------- #
# Ungoverned pipeline (NO_AGENT control arm)
# --------------------------------------------------------------------------- #


def _run_ungoverned(
    db: Session, audit: AuditLogger, job: Job, run: AgentRun, enable_diarization: bool
) -> None:
    """The control arm: identical models, no governance.

    Deliberately calls the ML services directly, bypassing the allow-list and the
    gate, and writes straight to the database. It exists to be measured against --
    it is what a competent-but-ungoverned build of this pipeline does, and the
    comparison in the report is exactly the difference between this function and
    `_run_governed`.
    """
    from pathlib import Path

    audit.agent(
        "run.ungoverned_control_arm",
        run_id=run.id,
        job_id=job.id,
        outcome="warning",
        detail={
            "note": "control arm for the agent-vs-no-agent study; no allow-list, no human gate",
            "gates_bypassed": ["persist_minutes"],
        },
    )

    stt_result = ml.get_stt().transcribe(Path(job.stored_path), job.language_hint)
    segments = [s.to_dict() for s in stt_result.segments]
    speaker_count = 0

    if enable_diarization:
        diarized = ml.get_diarizer().diarize(Path(job.stored_path), stt_result.segments)
        segments = [s.to_dict() for s in diarized.segments]
        speaker_count = diarized.speaker_count

    stt_payload = {
        "text": stt_result.text,
        "detected_languages": stt_result.detected_languages,
        "is_code_mixed": stt_result.is_code_mixed,
        "duration_seconds": stt_result.duration_seconds,
        "model_name": stt_result.model_name,
    }
    transcript = _persist_transcript(
        db, audit, job, run, stt_payload, segments, enable_diarization, speaker_count
    )

    segmentation = ml.get_segmenter().segment(transcript.text)
    blocks = [
        {
            "position": b.position,
            "title": b.title,
            "text": b.text,
            "start_char": b.start_char,
            "end_char": b.end_char,
            "confidence": b.confidence,
        }
        for b in segmentation.blocks
    ]
    _persist_blocks(db, transcript, blocks)

    extraction = ml.get_extractor().extract(segmentation.blocks)

    # No gate: straight to the database.
    from backend.app.models import ActionItem, Decision

    block_ids = {b.position: b.id for b in transcript.agenda_blocks}
    for d in extraction.decisions:
        db.add(
            Decision(
                job_id=job.id,
                agenda_block_id=block_ids.get(d.agenda_position),
                text=d.text,
                confidence=d.confidence,
                status=ItemStatus.PROPOSED,
                source_tool=extraction.model_name,
                evidence_quote=d.evidence_quote,
                run_id=run.id,
            )
        )
    for a in extraction.actions:
        db.add(
            ActionItem(
                job_id=job.id,
                agenda_block_id=block_ids.get(a.agenda_position),
                text=a.text,
                owner_name=a.owner_name,
                deadline=a.deadline,
                confidence=a.confidence,
                status=ItemStatus.PROPOSED,
                source_tool=extraction.model_name,
                evidence_quote=a.evidence_quote,
                run_id=run.id,
            )
        )
    db.flush()

    run.plan = [
        {"stage": s, "governed": False}
        for s in ["transcribe", "diarize", "segment", "extract", "persist"]
    ]
    _mark_ready(db, job, run)


# --------------------------------------------------------------------------- #
# Persistence helpers (system-owned, not agent-owned)
# --------------------------------------------------------------------------- #


def _persist_transcript(
    db: Session,
    audit: AuditLogger,
    job: Job,
    run: AgentRun,
    stt: dict[str, Any],
    segments: list[dict[str, Any]],
    diarization_enabled: bool,
    speaker_count: int,
) -> Transcript:
    """Scrub, scan, then store. PII never reaches the database un-redacted."""
    raw_text = stt["text"]

    # Injection scan runs on the RAW text so the audit record reflects what was
    # actually said, and so scrubbing cannot mask an attack pattern.
    injection = scan(raw_text)
    if injection.is_suspicious:
        for finding in injection.findings:
            injection_attempts_total.labels(rule=finding.rule, severity=finding.severity).inc()
        audit.system(
            "security.injection_detected",
            job_id=job.id,
            run_id=run.id,
            outcome="flagged",
            resource_type="transcript",
            detail={
                **injection.as_detail(),
                "excerpts": [f.excerpt[:200] for f in injection.findings[:5]],
                "note": "content is fenced as untrusted data; write actions remain gated",
            },
        )
        log.warning(
            "injection_detected",
            job_id=job.id,
            rules=sorted({f.rule for f in injection.findings}),
            severity=injection.max_severity,
        )

    scrubbed = pii.scrub_text(raw_text, enabled=settings.pii_scrubbing_enabled)
    scrubbed_segments, segment_redactions = pii.scrub_segments(
        segments, enabled=settings.pii_scrubbing_enabled
    )
    total_redactions = scrubbed.count + segment_redactions

    for kind, count in scrubbed.kinds.items():
        pii_redactions_total.labels(kind=kind).inc(count)

    if total_redactions:
        audit.system(
            "privacy.pii_redacted",
            job_id=job.id,
            run_id=run.id,
            resource_type="transcript",
            detail={"count": total_redactions, "kinds": scrubbed.kinds},
        )

    existing = db.query(Transcript).filter(Transcript.job_id == job.id).one_or_none()
    if existing is not None:
        db.delete(existing)
        db.flush()

    transcript = Transcript(
        job_id=job.id,
        text=scrubbed.text,
        detected_languages=stt.get("detected_languages", []),
        is_code_mixed=bool(stt.get("is_code_mixed", False)),
        duration_seconds=stt.get("duration_seconds"),
        model_name=stt.get("model_name", "unknown"),
        segments=scrubbed_segments,
        diarization_enabled=diarization_enabled,
        pii_redaction_count=total_redactions,
    )
    db.add(transcript)
    db.flush()

    audit.system(
        "pipeline.transcript_stored",
        job_id=job.id,
        run_id=run.id,
        resource_type="transcript",
        resource_id=transcript.id,
        detail={
            "model": transcript.model_name,
            "characters": len(transcript.text),
            "segments": len(scrubbed_segments),
            "languages": transcript.detected_languages,
            "code_mixed": transcript.is_code_mixed,
            "speaker_count": speaker_count,
            "pii_redactions": total_redactions,
        },
    )
    return transcript


def _persist_blocks(db: Session, transcript: Transcript, blocks: list[dict[str, Any]]) -> None:
    db.query(AgendaBlock).filter(AgendaBlock.transcript_id == transcript.id).delete()
    db.flush()
    for b in blocks:
        db.add(
            AgendaBlock(
                transcript_id=transcript.id,
                position=int(b["position"]),
                title=str(b["title"])[:512],
                text=str(b["text"]),
                start_char=int(b.get("start_char", 0)),
                end_char=int(b.get("end_char", 0)),
                confidence=float(b.get("confidence", 0.0)),
            )
        )
    db.flush()
    db.refresh(transcript)


# --------------------------------------------------------------------------- #
# Terminal states
# --------------------------------------------------------------------------- #


def _mark_ready(db: Session, job: Job, run: AgentRun) -> None:
    run.status = RunStatus.COMPLETED
    job.status = JobStatus.READY_FOR_REVIEW
    job.error_message = None
    db.flush()


def _fail(db: Session, audit: AuditLogger, job: Job | None, run: AgentRun, exc: Exception) -> None:
    run.status = RunStatus.FAILED
    run.error_message = f"{type(exc).__name__}: {exc}"[:2000]
    if job is not None:
        job.status = JobStatus.FAILED
        job.error_message = run.error_message
    audit.agent(
        "run.failed",
        run_id=run.id,
        job_id=run.job_id,
        outcome="error",
        resource_type="agent_run",
        resource_id=run.id,
        detail={"error_type": type(exc).__name__, "error": str(exc)[:500]},
    )
    log.error("run_failed", run_id=run.id, error=str(exc), error_type=type(exc).__name__)
    db.flush()


def _finalise(db: Session, run: AgentRun, started: float) -> None:
    elapsed = time.perf_counter() - started
    run.finished_at = _now() if run.status is not RunStatus.AWAITING_APPROVAL else None
    run.duration_ms = round(elapsed * 1000, 2)
    agent_runs_total.labels(mode=run.mode.value, status=run.status.value).inc()
    agent_run_duration_seconds.labels(mode=run.mode.value).observe(elapsed)
    db.flush()


# --------------------------------------------------------------------------- #
# Export (always gated)
# --------------------------------------------------------------------------- #


def request_export(
    db: Session,
    audit: AuditLogger,
    *,
    job: Job,
    actor_id: str,
    fmt: str,
    include_unapproved: bool = False,
) -> tuple[AgentRun, Any]:
    """Ask to export. Returns (run, gate_or_result).

    Export is an `external` side effect, so it always stops at a human gate --
    there is no code path that exports without a ruling. The gate payload carries
    the exact format and scope, so approving a CSV of approved items cannot be
    replayed as a JSON dump of everything.
    """
    run = AgentRun(job_id=job.id, mode=RunMode.AGENT, status=RunStatus.RUNNING)
    db.add(run)
    db.flush()

    token = run_id_ctx.set(run.id)
    started = time.perf_counter()

    audit.human(
        "export.requested",
        user_id=actor_id,
        job_id=job.id,
        run_id=run.id,
        resource_type="agent_run",
        resource_id=run.id,
        detail={"format": fmt, "include_unapproved": include_unapproved},
    )

    arguments = {"job_id": job.id, "format": fmt, "include_unapproved": include_unapproved}
    try:
        ctx = _context(db, audit, job, run)
        result = _invoke(ctx, "export_actions", arguments)
    except ApprovalRequired as required:
        scope = "ALL non-rejected items" if include_unapproved else "reviewer-approved items only"
        gate = gates.open_gate(
            db,
            audit,
            job_id=job.id,
            run_id=run.id,
            action=required.tool,
            summary=(
                f"Export {scope} from '{job.title or job.original_filename}' as "
                f"{fmt.upper()}. This releases meeting data out of the system."
            ),
            payload=required.payload,
            risk="high" if include_unapproved else required.risk,
        )
        run.status = RunStatus.AWAITING_APPROVAL
        db.flush()
        _finalise(db, run, started)
        run_id_ctx.reset(token)
        return run, gate
    except Exception as exc:
        _fail(db, audit, job, run, exc)
        _finalise(db, run, started)
        run_id_ctx.reset(token)
        raise

    # Only reachable if a policy change ever un-gates export; keep it correct anyway.
    _sync_counters(run, ctx)
    run.status = RunStatus.COMPLETED
    _finalise(db, run, started)
    run_id_ctx.reset(token)
    return run, result
