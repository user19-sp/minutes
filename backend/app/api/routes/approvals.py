"""Human approval gates -- the human-in-the-loop control point."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Query, status

from backend.app.agent import gates, orchestrator
from backend.app.api.deps import Audit, CurrentUser, DbSession, RequireReviewer
from backend.app.models import ApprovalRequest, ApprovalStatus, Job, Role
from backend.app.schemas import ApprovalDecision, ApprovalOut

router = APIRouter(prefix="/approvals", tags=["approvals"])


def _visible(db: DbSession, user: CurrentUser, gate_id: str) -> ApprovalRequest:
    gate = db.get(ApprovalRequest, gate_id)
    if gate is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Approval not found.")
    job = db.get(Job, gate.job_id)
    if job is None or (job.owner_id != user.id and user.role is not Role.ADMIN):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Approval not found.")
    return gate


@router.get("", response_model=list[ApprovalOut], summary="Approval queue")
def list_approvals(
    db: DbSession,
    user: CurrentUser,
    gate_status: Annotated[ApprovalStatus | None, Query(alias="status")] = ApprovalStatus.PENDING,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[ApprovalRequest]:
    query = db.query(ApprovalRequest).join(Job, Job.id == ApprovalRequest.job_id)
    if user.role is not Role.ADMIN:
        query = query.filter(Job.owner_id == user.id)
    if gate_status is not None:
        query = query.filter(ApprovalRequest.status == gate_status)
    return query.order_by(ApprovalRequest.created_at.desc()).limit(limit).all()


@router.get("/{approval_id}", response_model=ApprovalOut, summary="Inspect one gate")
def get_approval(
    approval_id: Annotated[str, Path(min_length=36, max_length=36)],
    db: DbSession,
    user: CurrentUser,
) -> ApprovalRequest:
    return _visible(db, user, approval_id)


@router.post(
    "/{approval_id}/decide",
    response_model=ApprovalOut,
    summary="Approve or reject a gated action",
)
def decide(
    approval_id: Annotated[str, Path(min_length=36, max_length=36)],
    payload: ApprovalDecision,
    db: DbSession,
    user: RequireReviewer,
    audit: Audit,
    resume: Annotated[bool, Query(description="Continue the run immediately on approval")] = True,
) -> ApprovalRequest:
    """Rule on a gate.

    On approval the suspended run is resumed by default, replaying exactly the
    payload shown in this gate. On rejection the run is aborted -- which is a
    successful outcome, not a failure: the human said no and nothing was written.
    """
    gate = _visible(db, user, approval_id)

    try:
        gates.decide_gate(
            db, audit, gate=gate, user=user, decision=payload.decision, note=payload.note
        )
    except gates.GateForbidden as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except gates.GateError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    if resume:
        from backend.app.models import AgentRun, RunStatus

        run = db.get(AgentRun, gate.run_id)
        still_pending = (
            db.query(ApprovalRequest)
            .filter(
                ApprovalRequest.run_id == gate.run_id,
                ApprovalRequest.status == ApprovalStatus.PENDING,
            )
            .count()
        )
        # Only resume once every gate on the run has been ruled on.
        if run is not None and run.status is RunStatus.AWAITING_APPROVAL and still_pending == 0:
            orchestrator.resume_run(db, audit, run=run, actor_id=user.id)

    db.refresh(gate)
    return gate


@router.post("/expire-stale", summary="Expire gates nobody decided (admin/ops)")
def expire_stale(db: DbSession, user: RequireReviewer, audit: Audit) -> dict:
    count = gates.expire_stale_gates(db, audit)
    return {"expired": count, "ttl_hours": gates.DEFAULT_TTL.total_seconds() / 3600}
