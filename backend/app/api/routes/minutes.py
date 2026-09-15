"""Reviewer editor API -- read the draft minutes, correct them, rule on each item."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, TypeVar

from fastapi import APIRouter, HTTPException, Path, status

from backend.app.agent import gates
from backend.app.api.deps import Audit, DbSession, OwnedJob, RequireReviewer
from backend.app.models import (
    ActionItem,
    AgentRun,
    Decision,
    ItemStatus,
    JobStatus,
)
from backend.app.observability.metrics import (
    reviewer_correction_seconds,
    reviewer_decisions_total,
)
from backend.app.schemas import (
    ActionItemOut,
    AgendaBlockOut,
    ApprovalOut,
    DecisionOut,
    JobOut,
    MinutesOut,
    ReviewAction,
    TranscriptOut,
)

router = APIRouter(tags=["review"])

_Item = TypeVar("_Item", Decision, ActionItem)

STATUS_MAP = {
    "approved": ItemStatus.APPROVED,
    "rejected": ItemStatus.REJECTED,
    "edited": ItemStatus.EDITED,
}


@router.get(
    "/jobs/{job_id}/minutes",
    response_model=MinutesOut,
    summary="Everything the reviewer editor needs for one meeting",
)
def get_minutes(job: OwnedJob, db: DbSession) -> MinutesOut:
    decisions = (
        db.query(Decision).filter(Decision.job_id == job.id).order_by(Decision.created_at).all()
    )
    actions = (
        db.query(ActionItem)
        .filter(ActionItem.job_id == job.id)
        .order_by(ActionItem.created_at)
        .all()
    )
    latest_run = (
        db.query(AgentRun)
        .filter(AgentRun.job_id == job.id)
        .order_by(AgentRun.started_at.desc())
        .first()
    )
    blocks = job.transcript.agenda_blocks if job.transcript else []

    return MinutesOut(
        job=JobOut.model_validate(job),
        transcript=TranscriptOut.model_validate(job.transcript) if job.transcript else None,
        agenda_blocks=[AgendaBlockOut.model_validate(b) for b in blocks],
        decisions=[DecisionOut.model_validate(d) for d in decisions],
        action_items=[ActionItemOut.model_validate(a) for a in actions],
        pending_approvals=[
            ApprovalOut.model_validate(g) for g in gates.pending_for_job(db, job.id)
        ],
        latest_run=latest_run,  # type: ignore[arg-type]
    )


@router.post(
    "/decisions/{decision_id}/review",
    response_model=DecisionOut,
    summary="Accept, correct or reject a decision",
)
def review_decision(
    decision_id: Annotated[str, Path(min_length=36, max_length=36)],
    payload: ReviewAction,
    db: DbSession,
    user: RequireReviewer,
    audit: Audit,
) -> Decision:
    item = _guard(db, db.get(Decision, decision_id), user)

    before = {"text": item.text, "status": item.status.value}
    if item.original_text is None:
        item.original_text = item.text  # preserve the model's proposal for error analysis

    if payload.status == "edited":
        item.text = (payload.text or item.text).strip()
    item.status = STATUS_MAP[payload.status]
    item.reviewed_by_id = user.id
    item.reviewed_at = datetime.now(UTC)
    item.review_note = payload.note
    item.review_seconds = payload.review_seconds
    db.flush()

    _record(audit, user, "decision", item.id, item.job_id, before, payload, item.status)
    return item


@router.post(
    "/actions/{action_id}/review",
    response_model=ActionItemOut,
    summary="Accept, correct or reject an action item",
)
def review_action(
    action_id: Annotated[str, Path(min_length=36, max_length=36)],
    payload: ReviewAction,
    db: DbSession,
    user: RequireReviewer,
    audit: Audit,
) -> ActionItem:
    item = _guard(db, db.get(ActionItem, action_id), user)

    before = {
        "text": item.text,
        "owner_name": item.owner_name,
        "deadline": item.deadline,
        "status": item.status.value,
    }
    if item.original_text is None:
        item.original_text = item.text
        item.original_owner_name = item.owner_name
        item.original_deadline = item.deadline

    if payload.status == "edited":
        item.text = (payload.text or item.text).strip()
        if payload.owner_name is not None:
            item.owner_name = payload.owner_name.strip() or None
        if payload.deadline is not None:
            item.deadline = payload.deadline.strip() or None

    item.status = STATUS_MAP[payload.status]
    item.reviewed_by_id = user.id
    item.reviewed_at = datetime.now(UTC)
    item.review_note = payload.note
    item.review_seconds = payload.review_seconds
    db.flush()

    _record(audit, user, "action_item", item.id, item.job_id, before, payload, item.status)
    _maybe_complete(db, item.job_id)
    return item


@router.get("/jobs/{job_id}/review-stats", summary="Reviewer effort for this meeting")
def review_stats(job: OwnedJob, db: DbSession) -> dict:
    """Reviewer-correction-time and edit-rate figures for the evaluation dossier."""
    decisions = db.query(Decision).filter(Decision.job_id == job.id).all()
    actions = db.query(ActionItem).filter(ActionItem.job_id == job.id).all()

    def bucket(items: list) -> dict:
        reviewed = [i for i in items if i.status is not ItemStatus.PROPOSED]
        times = [i.review_seconds for i in reviewed if i.review_seconds is not None]
        edited = sum(1 for i in reviewed if i.status is ItemStatus.EDITED)
        rejected = sum(1 for i in reviewed if i.status is ItemStatus.REJECTED)
        return {
            "total": len(items),
            "reviewed": len(reviewed),
            "accepted_unchanged": sum(1 for i in reviewed if i.status is ItemStatus.APPROVED),
            "edited": edited,
            "rejected": rejected,
            # Precision proxy: share of proposals a human kept as-is.
            "accept_rate": round(
                sum(1 for i in reviewed if i.status is ItemStatus.APPROVED) / len(reviewed), 3
            )
            if reviewed
            else None,
            "edit_rate": round(edited / len(reviewed), 3) if reviewed else None,
            "mean_review_seconds": round(sum(times) / len(times), 2) if times else None,
            "total_review_seconds": round(sum(times), 2) if times else None,
        }

    return {
        "job_id": job.id,
        "decisions": bucket(decisions),
        "action_items": bucket(actions),
    }


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _guard(db: DbSession, item: _Item | None, user) -> _Item:
    """Return the item if this user may review it, else 404.

    Returns rather than merely checking so callers get a non-optional value without
    an `assert` -- asserts are stripped under `python -O`, which would turn a
    missing item into an AttributeError in production.
    """
    from backend.app.models import Job, Role

    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Item not found.")
    job = db.get(Job, item.job_id)
    if job is None or (job.owner_id != user.id and user.role is not Role.ADMIN):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Item not found.")
    return item


def _record(
    audit,
    user,
    item_type: str,
    item_id: str,
    job_id: str,
    before: dict,
    payload: ReviewAction,
    new_status: ItemStatus,
) -> None:
    reviewer_decisions_total.labels(item_type=item_type, status=payload.status).inc()
    if payload.review_seconds is not None:
        reviewer_correction_seconds.labels(item_type=item_type).observe(payload.review_seconds)

    audit.human(
        f"review.{item_type}.{payload.status}",
        user_id=user.id,
        job_id=job_id,
        resource_type=item_type,
        resource_id=item_id,
        duration_ms=(payload.review_seconds or 0) * 1000,
        detail={
            "before": before,
            "after": {"status": new_status.value, "text": payload.text},
            "note": payload.note,
            "review_seconds": payload.review_seconds,
        },
    )


def _maybe_complete(db: DbSession, job_id: str) -> None:
    """Mark the meeting complete once nothing is left proposed."""
    from backend.app.models import Job

    outstanding = (
        db.query(ActionItem)
        .filter(ActionItem.job_id == job_id, ActionItem.status == ItemStatus.PROPOSED)
        .count()
        + db.query(Decision)
        .filter(Decision.job_id == job_id, Decision.status == ItemStatus.PROPOSED)
        .count()
    )
    job = db.get(Job, job_id)
    if job is not None and outstanding == 0 and job.status is JobStatus.READY_FOR_REVIEW:
        job.status = JobStatus.COMPLETED
        db.flush()
