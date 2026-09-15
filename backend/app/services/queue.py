"""Job queue for orchestrator runs.

Why this exists
---------------
A run used to execute inside the HTTP request. That is fine while the baseline
STT just reads a text file, but Whisper on a 30-minute recording takes minutes,
and nginx closes the connection at 300s. The browser would give up on exactly the
realistic input a demo wants to show.

So `POST /jobs/{id}/runs` now returns immediately with a QUEUED run, and a worker
process does the slow part. The reviewer editor already renders every job state,
so it just polls.

Design
------
**The run row is the queue entry.** A separate queue table would be a second
source of truth about the same work and the two would drift; `agent_runs` already
carries mode, status, timings and the audit correlation id.

**Claiming is atomic.** On PostgreSQL, `SELECT ... FOR UPDATE SKIP LOCKED` is the
standard pattern: two workers polling at the same instant cannot take the same
run, and neither blocks the other. SQLite has no SKIP LOCKED, so the dev path
uses a guarded conditional UPDATE instead -- correct for one worker, which is all
single-file SQLite should ever have.

No Redis, no Celery. The database is already in the stack and already durable;
adding a broker would add a service, a failure mode and a second thing to deploy
for work that is measured in jobs per hour.

**Crash recovery.** A worker that dies mid-run leaves the row RUNNING with a
`claimed_at`. `requeue_abandoned` returns those to QUEUED once the lease expires,
so work is retried rather than lost, up to `MAX_ATTEMPTS`.
"""

from __future__ import annotations

import os
import socket
import threading
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from backend.app.agent.audit import AuditLogger
from backend.app.db import SessionLocal, engine
from backend.app.models import AgentRun, Job, JobStatus, RunMode, RunStatus
from backend.app.observability.logging import get_logger, run_id_ctx
from backend.app.observability.metrics import (
    queue_claim_latency_seconds,
    queue_depth,
    queue_runs_total,
)

log = get_logger("queue")

#: How long a worker may hold a run before it is considered abandoned.
LEASE = timedelta(minutes=30)

#: A run that has failed this many times is left FAILED rather than retried
#: forever. A poison job must not occupy the queue.
MAX_ATTEMPTS = 3

POLL_INTERVAL_SECONDS = float(os.getenv("MOM_WORKER_POLL_SECONDS", "2"))


def worker_identity() -> str:
    """Identifies which worker holds a run, for debugging a stuck queue."""
    return f"{socket.gethostname()}:{os.getpid()}"


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(dt: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes; normalise before arithmetic."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Enqueue
# --------------------------------------------------------------------------- #


def enqueue(
    db: Session,
    audit: AuditLogger,
    *,
    job: Job,
    actor_id: str,
    mode: RunMode = RunMode.AGENT,
    enable_diarization: bool = False,
) -> AgentRun:
    """Create a QUEUED run and return immediately. No ML work happens here."""
    run = AgentRun(
        job_id=job.id,
        mode=mode,
        status=RunStatus.QUEUED,
        enable_diarization=enable_diarization,
        queued_at=_now(),
    )
    db.add(run)
    job.status = JobStatus.QUEUED
    db.flush()

    queue_runs_total.labels(mode=mode.value, outcome="enqueued").inc()
    audit.human(
        "run.queued",
        user_id=actor_id,
        job_id=job.id,
        run_id=run.id,
        resource_type="agent_run",
        resource_id=run.id,
        detail={"mode": mode.value, "enable_diarization": enable_diarization},
    )
    log.info("run_queued", run_id=run.id, job_id=job.id, mode=mode.value)
    return run


# --------------------------------------------------------------------------- #
# Claim
# --------------------------------------------------------------------------- #


def claim_next(db: Session, worker: str) -> AgentRun | None:
    """Atomically take the oldest queued run, or return None.

    Two workers polling simultaneously must never take the same run -- that would
    run the pipeline twice and open two approval gates for one meeting.
    """
    if engine.dialect.name == "postgresql":
        return _claim_postgres(db, worker)
    return _claim_generic(db, worker)


def _claim_postgres(db: Session, worker: str) -> AgentRun | None:
    """SKIP LOCKED: the canonical Postgres queue. Concurrent workers step past
    rows their peers are already holding instead of blocking on them."""
    run = db.execute(
        select(AgentRun)
        .where(AgentRun.status == RunStatus.QUEUED)
        .order_by(AgentRun.queued_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    ).scalar_one_or_none()

    if run is None:
        return None

    run.status = RunStatus.RUNNING
    run.claimed_at = _now()
    run.claimed_by = worker
    run.attempts += 1
    db.commit()
    db.refresh(run)
    return run


def _claim_generic(db: Session, worker: str) -> AgentRun | None:
    """SQLite has no SKIP LOCKED. A conditional UPDATE guarded on the row still
    being QUEUED is safe here: only one statement can win that transition, and
    single-file SQLite should never be running multiple workers anyway."""
    candidate = db.execute(
        select(AgentRun)
        .where(AgentRun.status == RunStatus.QUEUED)
        .order_by(AgentRun.queued_at)
        .limit(1)
    ).scalar_one_or_none()

    if candidate is None:
        return None

    claimed = db.execute(
        update(AgentRun)
        .where(AgentRun.id == candidate.id, AgentRun.status == RunStatus.QUEUED)
        .values(
            status=RunStatus.RUNNING,
            claimed_at=_now(),
            claimed_by=worker,
            attempts=AgentRun.attempts + 1,
        )
    )
    db.commit()

    if claimed.rowcount == 0:
        return None  # someone else got there first
    db.refresh(candidate)
    return candidate


# --------------------------------------------------------------------------- #
# Recovery
# --------------------------------------------------------------------------- #


def requeue_abandoned(db: Session, audit: AuditLogger, lease: timedelta = LEASE) -> int:
    """Return runs whose worker died back to the queue.

    Without this a crashed worker silently strands a meeting in RUNNING forever.
    """
    cutoff = _now() - lease
    stuck = (
        db.execute(
            select(AgentRun).where(
                AgentRun.status == RunStatus.RUNNING,
                AgentRun.claimed_at.is_not(None),
            )
        )
        .scalars()
        .all()
    )

    recovered = 0
    for run in stuck:
        claimed = _aware(run.claimed_at)
        if claimed is None or claimed > cutoff:
            continue

        if run.attempts >= MAX_ATTEMPTS:
            run.status = RunStatus.FAILED
            run.error_message = (
                f"Abandoned by worker {run.claimed_by} and retried {run.attempts} times; giving up."
            )
            job = db.get(Job, run.job_id)
            if job is not None:
                job.status = JobStatus.FAILED
                job.error_message = run.error_message
            queue_runs_total.labels(mode=run.mode.value, outcome="abandoned").inc()
            audit.system(
                "run.abandoned",
                job_id=run.job_id,
                run_id=run.id,
                outcome="error",
                resource_type="agent_run",
                resource_id=run.id,
                detail={"attempts": run.attempts, "worker": run.claimed_by},
            )
        else:
            run.status = RunStatus.QUEUED
            run.claimed_at = None
            run.claimed_by = None
            run.queued_at = _now()
            queue_runs_total.labels(mode=run.mode.value, outcome="requeued").inc()
            audit.system(
                "run.requeued",
                job_id=run.job_id,
                run_id=run.id,
                outcome="warning",
                resource_type="agent_run",
                resource_id=run.id,
                detail={"attempts": run.attempts, "previous_worker": run.claimed_by},
            )
        recovered += 1

    if recovered:
        db.commit()
        log.warning("runs_recovered", count=recovered)
    return recovered


def depth(db: Session) -> int:
    return db.query(AgentRun).filter(AgentRun.status == RunStatus.QUEUED).count()


# --------------------------------------------------------------------------- #
# Worker
# --------------------------------------------------------------------------- #


def execute_claimed(db: Session, audit: AuditLogger, run: AgentRun) -> AgentRun:
    """Run the orchestrator for an already-claimed run."""
    from backend.app.agent import orchestrator

    job = db.get(Job, run.job_id)
    if job is None:
        run.status = RunStatus.FAILED
        run.error_message = "Job no longer exists."
        db.commit()
        return run

    token = run_id_ctx.set(run.id)
    try:
        orchestrator.execute_run(db, audit, job=job, run=run)
        db.commit()
    finally:
        run_id_ctx.reset(token)

    queue_runs_total.labels(mode=run.mode.value, outcome=run.status.value).inc()
    return run


def run_worker(stop: threading.Event | None = None, poll: float | None = None) -> None:
    """Poll the queue until stopped. This is the worker process's main loop."""
    worker = worker_identity()
    interval = poll if poll is not None else POLL_INTERVAL_SECONDS
    stop = stop or threading.Event()

    log.info("worker_started", worker=worker, poll_seconds=interval)
    last_sweep = 0.0

    while not stop.is_set():
        try:
            with SessionLocal() as db:
                audit = AuditLogger(db)

                # Recover abandoned work occasionally, not on every poll.
                if time.monotonic() - last_sweep > 60:
                    last_sweep = time.monotonic()
                    requeue_abandoned(db, audit)

                queue_depth.set(depth(db))

                started = time.perf_counter()
                run = claim_next(db, worker)
                if run is None:
                    stop.wait(interval)
                    continue

                queue_claim_latency_seconds.observe(time.perf_counter() - started)
                log.info("run_claimed", run_id=run.id, job_id=run.job_id, worker=worker)
                execute_claimed(db, audit, run)
                log.info("run_finished", run_id=run.id, status=run.status.value)

        except Exception as exc:  # a worker must not die on one bad job
            log.exception("worker_error", error=str(exc), error_type=type(exc).__name__)
            stop.wait(interval)

    log.info("worker_stopped", worker=worker)
