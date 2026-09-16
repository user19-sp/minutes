"""The registration allow-list.

`docs/threat-model.md` assumes "no public registration". This module is what makes
that true rather than merely stated: an administrator names the addresses that may
create an account, and `POST /auth/register` refuses everything else.

An explicit address list rather than a domain rule. A domain rule (`*@company.com`)
needs a company domain to demonstrate, and amounts to a line in a config file --
nothing an examiner can watch working. A list is administered through the UI, so
the control can be shown: add an address, watch registration succeed; remove it,
watch it refuse.

Addresses are normalised to lowercase on both write and read, because email
local-parts are case-insensitive in every practical mail system and a list that
lets `Priya@x.com` through but not `priya@x.com` is a bug waiting to be reported.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.agent.audit import AuditLogger
from backend.app.config import settings
from backend.app.models import ActorType, ApprovedEmail, User

__all__ = [
    "AlreadyApproved",
    "approve",
    "is_allowed",
    "list_approved",
    "mark_used",
    "revoke",
]


class AlreadyApproved(Exception):
    """The address is already on the list. Callers map this to HTTP 409."""


def normalise(email: str) -> str:
    return email.strip().lower()


def is_allowed(db: Session, email: str) -> bool:
    """May this address register?

    In `open` mode everything is allowed and the list is ignored entirely, so the
    feature can be turned off without emptying the table.
    """
    if settings.registration_mode == "open":
        return True
    return (
        db.execute(
            select(ApprovedEmail).where(ApprovedEmail.email == normalise(email))
        ).scalar_one_or_none()
        is not None
    )


def approve(
    db: Session,
    audit: AuditLogger,
    *,
    email: str,
    admin: User,
    note: str | None = None,
) -> ApprovedEmail:
    """Add an address to the list. Admin-only; enforced at the route."""
    address = normalise(email)

    existing = db.execute(
        select(ApprovedEmail).where(ApprovedEmail.email == address)
    ).scalar_one_or_none()
    if existing is not None:
        raise AlreadyApproved(f"{address} is already approved.")

    entry = ApprovedEmail(email=address, note=note, added_by_id=admin.id)
    db.add(entry)
    db.flush()

    audit.human(
        "registration.email_approved",
        user_id=admin.id,
        resource_type="approved_email",
        resource_id=entry.id,
        detail={"email": address, "note": note},
    )
    return entry


def revoke(db: Session, audit: AuditLogger, *, entry: ApprovedEmail, admin: User) -> None:
    """Remove an address from the list.

    This stops future registrations only. An account already created with the
    address keeps working -- deactivate the user for that, which is a different
    action with different consequences and should not happen as a side effect of
    tidying a list.
    """
    address = entry.email
    entry_id = entry.id
    db.delete(entry)
    db.flush()

    audit.human(
        "registration.email_revoked",
        user_id=admin.id,
        resource_type="approved_email",
        resource_id=entry_id,
        detail={
            "email": address,
            "note": "future registrations only; existing accounts are unaffected",
        },
    )


def mark_used(db: Session, email: str) -> None:
    """Stamp the entry when someone registers with it, so an admin can see which
    invitations are still outstanding."""
    entry = db.execute(
        select(ApprovedEmail).where(ApprovedEmail.email == normalise(email))
    ).scalar_one_or_none()
    if entry is not None and entry.used_at is None:
        entry.used_at = datetime.now(UTC)
        db.flush()


def list_approved(db: Session, limit: int = 200, offset: int = 0) -> list[ApprovedEmail]:
    return list(
        db.execute(
            select(ApprovedEmail)
            .order_by(ApprovedEmail.added_at.desc())
            .limit(limit)
            .offset(offset)
        ).scalars()
    )


def record_refusal(db: Session, audit: AuditLogger, email: str) -> None:
    """Audit a refused registration.

    Written independently, because the route turns this into a 403 and the request
    rolls back -- the evidence of the refusal has to survive that.
    """
    audit.refusal(
        "registration.refused",
        ActorType.SYSTEM,
        actor_id=None,
        detail={"email": normalise(email), "reason": "not_on_approved_list"},
    )
