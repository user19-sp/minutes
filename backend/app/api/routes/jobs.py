"""Ingestion and job lifecycle."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile, status

from backend.app.api.deps import Audit, CurrentUser, DbSession, OwnedJob
from backend.app.models import ActorType, Job, JobStatus, Role
from backend.app.observability.metrics import jobs_total
from backend.app.schemas import JobOut, MessageOut, TranscriptOut
from backend.app.services import ingestion

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.post(
    "",
    response_model=JobOut,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a meeting recording",
)
def create_job(
    db: DbSession,
    user: CurrentUser,
    audit: Audit,
    file: Annotated[UploadFile, File(description="Audio recording of the meeting")],
    title: Annotated[str | None, Form(max_length=512)] = None,
    language_hint: Annotated[str | None, Form(max_length=32)] = None,
) -> Job:
    try:
        stored = ingestion.store_upload(file.file, file.filename or "upload", file.content_type)
    except ingestion.UploadRejected as exc:
        audit.refusal(
            "job.upload_rejected",
            ActorType.HUMAN,
            actor_id=user.id,
            detail={
                "reason": exc.code,
                "message": str(exc),
                "filename": ingestion.sanitise_filename(file.filename or ""),
                "declared_content_type": file.content_type,
            },
        )
        code = (
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
            if exc.code == "too_large"
            else status.HTTP_415_UNSUPPORTED_MEDIA_TYPE
            if exc.code in ("unsupported_extension", "unsupported_content_type")
            else status.HTTP_400_BAD_REQUEST
        )
        raise HTTPException(status_code=code, detail=str(exc)) from exc

    job = Job(
        owner_id=user.id,
        original_filename=stored.safe_name,
        stored_path=str(stored.path),
        content_type=(file.content_type or "application/octet-stream").split(";")[0],
        size_bytes=stored.size_bytes,
        sha256=stored.sha256,
        title=title,
        language_hint=language_hint,
        status=JobStatus.UPLOADED,
    )
    db.add(job)
    db.flush()

    jobs_total.labels(status="uploaded").inc()
    audit.human(
        "job.created",
        user_id=user.id,
        job_id=job.id,
        resource_type="job",
        resource_id=job.id,
        detail={
            "filename": job.original_filename,
            "size_bytes": job.size_bytes,
            "sha256": job.sha256,
            "content_type": job.content_type,
            "language_hint": job.language_hint,
        },
    )
    return job


@router.get("", response_model=list[JobOut], summary="List your meetings")
def list_jobs(
    db: DbSession,
    user: CurrentUser,
    job_status: Annotated[JobStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[Job]:
    query = db.query(Job)
    if user.role is not Role.ADMIN:
        query = query.filter(Job.owner_id == user.id)
    if job_status is not None:
        query = query.filter(Job.status == job_status)
    return query.order_by(Job.created_at.desc()).limit(limit).offset(offset).all()


@router.get("/{job_id}", response_model=JobOut, summary="Fetch one meeting")
def get_job(job: OwnedJob) -> Job:
    return job


@router.get(
    "/{job_id}/transcript",
    response_model=TranscriptOut,
    summary="Fetch the PII-scrubbed transcript",
)
def get_transcript(job: OwnedJob):
    if job.transcript is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No transcript yet. Start a run for this job first.",
        )
    return job.transcript


@router.delete(
    "/{job_id}",
    response_model=MessageOut,
    summary="Delete a meeting and its derived data",
)
def delete_job(job: OwnedJob, db: DbSession, user: CurrentUser, audit: Audit) -> MessageOut:
    """Right-to-erasure path: removes the recording from disk and cascades the
    derived rows. Audit events are deliberately retained -- the trail records that
    a deletion happened, without retaining the content."""
    job_id, filename = job.id, job.original_filename
    try:
        removed = ingestion.delete_upload(job.stored_path)
    except ingestion.UploadRejected:
        removed = False

    db.delete(job)
    db.flush()

    audit.human(
        "job.deleted",
        user_id=user.id,
        job_id=job_id,
        resource_type="job",
        resource_id=job_id,
        detail={"filename": filename, "recording_removed": removed},
    )
    return MessageOut(detail=f"Meeting {job_id} deleted.")
