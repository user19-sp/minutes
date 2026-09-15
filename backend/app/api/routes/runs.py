"""Orchestrator runs and the governance surface."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Response, status

from backend.app.agent import orchestrator
from backend.app.agent.registry import registry
from backend.app.agent.tools import AGENT_TOOLS, PIPELINE_TOOLS
from backend.app.api.deps import Audit, CurrentUser, DbSession, OwnedJob, RequireReviewer
from backend.app.config import settings
from backend.app.models import AgentRun, JobStatus, RunMode, RunStatus
from backend.app.schemas import AgentRunOut, RunStartRequest, ToolSpecOut
from backend.app.services import queue

router = APIRouter(tags=["governance"])


# --------------------------------------------------------------------------- #
# Capability surface
# --------------------------------------------------------------------------- #


@router.get(
    "/governance/tools",
    response_model=list[ToolSpecOut],
    summary="The agent's complete allow-list",
)
def list_tools(_: CurrentUser) -> list[dict]:
    """Every tool the orchestrator can call, and whether it is gated.

    Exposed so a reviewer (or an examiner) can read the agent's capability surface
    directly from the running system rather than taking the report's word for it.
    """
    return [spec.public() for spec in registry.specs()]


@router.get("/governance/policy", summary="Governance configuration in force")
def governance_policy(_: CurrentUser) -> dict:
    specs = registry.specs()
    return {
        "max_tool_calls_per_run": settings.agent_max_tool_calls,
        "auto_approve_threshold": settings.agent_auto_approve_threshold,
        "auto_approve_enabled": settings.agent_auto_approve_threshold <= 1.0,
        "pii_scrubbing_enabled": settings.pii_scrubbing_enabled,
        "registered_tools": len(specs),
        "gated_tools": sorted(s.name for s in specs if s.requires_approval),
        "ungated_read_tools": sorted(s.name for s in specs if not s.requires_approval),
        "tools_by_mode": {
            "agent": sorted(AGENT_TOOLS),
            "no_agent": sorted(PIPELINE_TOOLS),
        },
        "notes": [
            "Tools with side_effect write/external cannot be registered without a gate.",
            "An approval is bound to a SHA-256 fingerprint of the exact arguments approved.",
            "Denied tool calls are audited and counted; they are evidence, not errors.",
        ],
    }


# --------------------------------------------------------------------------- #
# Runs
# --------------------------------------------------------------------------- #


@router.post(
    "/jobs/{job_id}/runs",
    response_model=AgentRunOut,
    status_code=status.HTTP_201_CREATED,
    summary="Start an orchestrator run",
    responses={202: {"description": "Run queued; a worker will execute it."}},
)
def start_run(
    job: OwnedJob,
    payload: RunStartRequest,
    db: DbSession,
    user: RequireReviewer,
    audit: Audit,
    response: Response,
) -> AgentRun:
    """Run the pipeline over an uploaded meeting.

    Two execution modes, chosen by configuration rather than by the caller:

    * **queued** (deployment) -- returns 202 immediately with a QUEUED run and a
      worker does the slow part. Necessary because real transcription takes
      minutes and the proxy closes the connection long before that.
    * **inline** (dev and tests) -- executes here and returns 201 with the
      finished run, so a test can assert on the outcome without polling.

    Either way the reviewer editor renders the same job states; it polls while a
    job is queued or running.
    """
    if job.status in (JobStatus.RUNNING, JobStatus.AWAITING_APPROVAL, JobStatus.QUEUED):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Job is already {job.status.value}; wait for it to finish or decide its gate.",
        )

    if settings.run_execution == "queued":
        run = queue.enqueue(
            db,
            audit,
            job=job,
            actor_id=user.id,
            mode=payload.mode,
            enable_diarization=payload.enable_diarization,
        )
        response.status_code = status.HTTP_202_ACCEPTED
        return run

    return orchestrator.start_run(
        db,
        audit,
        job=job,
        actor_id=user.id,
        mode=payload.mode,
        enable_diarization=payload.enable_diarization,
    )


@router.get("/queue", summary="Queue depth and in-flight runs")
def queue_status(db: DbSession, _: CurrentUser) -> dict:
    """Operational view of the queue -- what is waiting and what is being worked on."""
    from backend.app.models import RunStatus as RS

    running = (
        db.query(AgentRun).filter(AgentRun.status == RS.RUNNING).order_by(AgentRun.claimed_at).all()
    )
    return {
        "execution_mode": settings.run_execution,
        "queued": queue.depth(db),
        "running": [
            {
                "run_id": r.id,
                "job_id": r.job_id,
                "worker": r.claimed_by,
                "claimed_at": r.claimed_at.isoformat() if r.claimed_at else None,
                "attempts": r.attempts,
            }
            for r in running
        ],
    }


@router.get(
    "/jobs/{job_id}/runs",
    response_model=list[AgentRunOut],
    summary="Run history for a meeting",
)
def list_runs(job: OwnedJob, db: DbSession) -> list[AgentRun]:
    return (
        db.query(AgentRun)
        .filter(AgentRun.job_id == job.id)
        .order_by(AgentRun.started_at.desc())
        .all()
    )


@router.get("/runs/{run_id}", response_model=AgentRunOut, summary="Fetch one run")
def get_run(
    run_id: Annotated[str, Path(min_length=36, max_length=36)],
    db: DbSession,
    user: CurrentUser,
) -> AgentRun:
    return _load_run(db, run_id, user)


@router.get("/runs/{run_id}/trace", summary="Step-by-step trace of a run")
def get_run_trace(
    run_id: Annotated[str, Path(min_length=36, max_length=36)],
    db: DbSession,
    user: CurrentUser,
) -> dict:
    """The ordered list of tool calls, denials and their outcomes.

    This is the traceability artefact: for any item in the minutes you can follow
    the run that produced it back through every governed step.
    """
    run = _load_run(db, run_id, user)
    return {
        "run_id": run.id,
        "job_id": run.job_id,
        "mode": run.mode.value,
        "status": run.status.value,
        "trace_id": run.trace_id,
        "tool_calls": run.tool_call_count,
        "denied_tool_calls": run.denied_tool_call_count,
        "duration_ms": run.duration_ms,
        "steps": run.plan or [],
    }


@router.post(
    "/runs/{run_id}/resume",
    response_model=AgentRunOut,
    summary="Resume a run suspended at a gate",
)
def resume_run(
    run_id: Annotated[str, Path(min_length=36, max_length=36)],
    db: DbSession,
    user: RequireReviewer,
    audit: Audit,
) -> AgentRun:
    run = _load_run(db, run_id, user)
    try:
        return orchestrator.resume_run(db, audit, run=run, actor_id=user.id)
    except orchestrator.OrchestratorError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.get(
    "/jobs/{job_id}/comparison",
    summary="Agent vs no-agent comparison for a meeting",
)
def run_comparison(job: OwnedJob, db: DbSession) -> dict:
    """Side-by-side figures for the two arms of the study.

    Feeds the comparison table in the final report: governed runs stop for a human
    and record every step; the control arm writes straight through.
    """
    runs = db.query(AgentRun).filter(AgentRun.job_id == job.id).all()

    def summarise(mode: RunMode) -> dict:
        arm = [r for r in runs if r.mode is mode]
        completed = [r for r in arm if r.status is RunStatus.COMPLETED]
        durations = [r.duration_ms for r in arm if r.duration_ms is not None]
        return {
            "runs": len(arm),
            "completed": len(completed),
            "failed": sum(1 for r in arm if r.status is RunStatus.FAILED),
            "suspended_at_gate": sum(1 for r in arm if r.status is RunStatus.AWAITING_APPROVAL),
            "aborted_by_human": sum(1 for r in arm if r.status is RunStatus.ABORTED),
            "total_tool_calls": sum(r.tool_call_count or 0 for r in arm),
            "total_denied_calls": sum(r.denied_tool_call_count or 0 for r in arm),
            "mean_duration_ms": round(sum(durations) / len(durations), 2) if durations else None,
            "traced_steps": sum(len(r.plan or []) for r in arm),
        }

    agent = summarise(RunMode.AGENT)
    control = summarise(RunMode.NO_AGENT)
    return {
        "job_id": job.id,
        "agent": agent,
        "no_agent_control": control,
        "interpretation": {
            "human_gates_enforced": agent["suspended_at_gate"] + agent["aborted_by_human"],
            "writes_without_human_review": control["completed"],
            "governance_overhead_ms": (
                round(agent["mean_duration_ms"] - control["mean_duration_ms"], 2)
                if agent["mean_duration_ms"] and control["mean_duration_ms"]
                else None
            ),
        },
    }


def _load_run(db: DbSession, run_id: str, user: CurrentUser) -> AgentRun:
    from backend.app.models import Job, Role

    run = db.get(AgentRun, run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found.")
    job = db.get(Job, run.job_id)
    if job is None or (job.owner_id != user.id and user.role is not Role.ADMIN):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found.")
    return run
