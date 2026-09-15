"""Read-only access to the audit trail.

There is no write, update or delete endpoint here, and that is deliberate: the
trail is append-only and the API surface must not offer a way to edit history.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Response

from backend.app.agent import audit as audit_service
from backend.app.api.deps import CurrentUser, DbSession, OwnedJob
from backend.app.models import AuditEvent, Role
from backend.app.schemas import AuditEventOut

router = APIRouter(tags=["audit"])


@router.get(
    "/jobs/{job_id}/audit",
    response_model=list[AuditEventOut],
    summary="Audit trail for one meeting",
)
def job_audit(
    job: OwnedJob,
    db: DbSession,
    action: Annotated[str | None, Query(description="Filter by action name")] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[AuditEvent]:
    return audit_service.get_trail(db, job_id=job.id, action=action, limit=limit, offset=offset)


@router.get(
    "/jobs/{job_id}/audit/export",
    summary="Download the trail as JSONL (evidence appendix)",
)
def job_audit_export(job: OwnedJob, db: DbSession) -> Response:
    events = audit_service.get_trail(db, job_id=job.id, limit=10_000)
    body = audit_service.export_trail_jsonl(events)
    return Response(
        content=body,
        media_type="application/x-ndjson",
        headers={"Content-Disposition": f'attachment; filename="audit_{job.id}.jsonl"'},
    )


@router.get("/runs/{run_id}/audit", response_model=list[AuditEventOut], summary="Trail for one run")
def run_audit(
    run_id: str,
    db: DbSession,
    user: CurrentUser,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> list[AuditEvent]:
    from fastapi import HTTPException, status

    from backend.app.models import AgentRun, Job

    run = db.get(AgentRun, run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found.")
    job = db.get(Job, run.job_id)
    if job is None or (job.owner_id != user.id and user.role is not Role.ADMIN):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found.")

    return audit_service.get_trail(db, run_id=run_id, limit=limit)


@router.get("/audit", response_model=list[AuditEventOut], summary="Global trail (admin only)")
def global_audit(
    db: DbSession,
    user: CurrentUser,
    action: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[AuditEvent]:
    from fastapi import HTTPException, status

    if user.role is not Role.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="The global audit trail is restricted to administrators.",
        )
    return audit_service.get_trail(db, action=action, limit=limit, offset=offset)
