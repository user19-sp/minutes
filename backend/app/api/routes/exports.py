"""Task export -- the only route by which data leaves the system, and always gated."""

from __future__ import annotations

from pathlib import Path as FsPath
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Response, status
from fastapi.responses import FileResponse

from backend.app.agent import orchestrator
from backend.app.api.deps import Audit, CurrentUser, DbSession, OwnedJob, RequireReviewer
from backend.app.config import settings
from backend.app.models import ExportRecord, Job, Role
from backend.app.schemas import ApprovalOut, ExportOut, ExportRequest

router = APIRouter(tags=["export"])

MEDIA_TYPES = {
    "csv": "text/csv",
    "json": "application/json",
    "tracker": "application/json",
}


@router.post(
    "/jobs/{job_id}/exports",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Request an export (opens a human approval gate)",
    responses={202: {"description": "Approval gate opened; no file written yet."}},
)
def request_export(
    job: OwnedJob,
    payload: ExportRequest,
    db: DbSession,
    user: RequireReviewer,
    audit: Audit,
    response: Response = None,  # type: ignore[assignment]
) -> dict:
    """Ask to export approved action items.

    Returns 202 with the gate that must be decided. Nothing is written to disk and
    nothing leaves the system until a reviewer approves that gate.
    """
    # Pre-flight: never ask a human to approve an action that cannot succeed.
    # Without this the gate opens, the reviewer approves, and the run then fails
    # with "nothing to export" -- which trains reviewers to click through gates.
    from backend.app.services.export import selectable_actions

    eligible = selectable_actions(db, job.id, payload.include_unapproved)
    if not eligible:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Nothing to export: no reviewer-approved action items for this meeting. "
                "Approve items in the reviewer editor first."
            ),
        )

    run, outcome = orchestrator.request_export(
        db,
        audit,
        job=job,
        actor_id=user.id,
        fmt=payload.format,
        include_unapproved=payload.include_unapproved,
    )

    if isinstance(outcome, dict):  # un-gated path; not reachable under current policy
        return {"status": "completed", "run_id": run.id, "result": outcome}

    return {
        "status": "awaiting_approval",
        "run_id": run.id,
        "approval": ApprovalOut.model_validate(outcome).model_dump(mode="json"),
        "next_step": f"POST /api/v1/approvals/{outcome.id}/decide",
    }


@router.get(
    "/jobs/{job_id}/exports",
    response_model=list[ExportOut],
    summary="Files produced for this meeting",
)
def list_exports(job: OwnedJob, db: DbSession) -> list[ExportRecord]:
    return (
        db.query(ExportRecord)
        .filter(ExportRecord.job_id == job.id)
        .order_by(ExportRecord.created_at.desc())
        .all()
    )


@router.get("/exports/{export_id}/download", summary="Download a produced export")
def download_export(
    export_id: Annotated[str, Path(min_length=36, max_length=36)],
    db: DbSession,
    user: CurrentUser,
    audit: Audit,
) -> FileResponse:
    record = db.get(ExportRecord, export_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Export not found.")

    job = db.get(Job, record.job_id)
    if job is None or (job.owner_id != user.id and user.role is not Role.ADMIN):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Export not found.")

    path = FsPath(record.stored_path).resolve()
    # Containment check: a stored path must still resolve inside the export dir.
    if not path.is_relative_to(settings.export_dir.resolve()) or not path.exists():
        raise HTTPException(
            status_code=status.HTTP_410_GONE, detail="Export file is no longer available."
        )

    audit.human(
        "export.downloaded",
        user_id=user.id,
        job_id=record.job_id,
        resource_type="export",
        resource_id=record.id,
        detail={"format": record.format, "sha256": record.sha256, "items": record.item_count},
    )
    return FileResponse(
        path,
        media_type=MEDIA_TYPES.get(record.format, "application/octet-stream"),
        filename=path.name,
        headers={"X-Content-SHA256": record.sha256},
    )
