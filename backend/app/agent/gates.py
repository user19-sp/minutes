"""Approval-gate state machine.

A gate is the point where an autonomous process stops and a human decides. The
state machine is small on purpose -- it is the safety property the whole project
rests on, so it must be obviously correct and fully testable:

    (no gate) --request--> PENDING --approve--> APPROVED  (terminal)
                              |----reject---->  REJECTED  (terminal)
                              |----expire---->  EXPIRED   (terminal)

Invariants enforced here and asserted in tests/security/test_approval_gates.py:

  I1  Only PENDING may be decided. Re-deciding a terminal gate is refused, so an
      approval cannot be replayed to authorise a second export.
  I2  Only a reviewer or admin may decide. A viewer is refused.
  I3  The decider is recorded with a timestamp, and the ruling is audited.
  I4  An approval authorises one fingerprinted payload, not a tool in general --
      see `registry.approval_key`. Changing the arguments invalidates the grant.
  I5  A gate belongs to exactly one run; grants never cross runs.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.agent.audit import AuditLogger
from backend.app.agent.registry import approval_key
from backend.app.models import (
    ActorType,
    ApprovalRequest,
    ApprovalStatus,
    Role,
    User,
)
from backend.app.observability.logging import get_logger
from backend.app.observability.metrics import (
    approval_gates_total,
    approval_wait_seconds,
    pending_approvals,
)

log = get_logger("agent.gates")

#: A gate left undecided this long is expired rather than left open forever.
DEFAULT_TTL = timedelta(hours=48)

DECIDING_ROLES = {Role.REVIEWER, Role.ADMIN}


class GateError(Exception):
    """Invalid gate transition. Callers map this to HTTP 409."""


class GateForbidden(Exception):
    """The actor may not decide this gate. Callers map this to HTTP 403."""


def _aware(dt: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; normalise before arithmetic."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def open_gate(
    db: Session,
    audit: AuditLogger,
    *,
    job_id: str,
    run_id: str,
    action: str,
    summary: str,
    payload: dict[str, Any],
    risk: str = "medium",
) -> ApprovalRequest:
    """Create a PENDING gate. Idempotent: an identical pending gate is reused, so a
    retried run does not spam the reviewer with duplicates."""
    existing = (
        db.execute(
            select(ApprovalRequest).where(
                ApprovalRequest.run_id == run_id,
                ApprovalRequest.action == action,
                ApprovalRequest.status == ApprovalStatus.PENDING,
            )
        )
        .scalars()
        .first()
    )

    if existing is not None and existing.payload == payload:
        return existing

    gate = ApprovalRequest(
        job_id=job_id,
        run_id=run_id,
        action=action,
        summary=summary,
        payload=payload,
        risk=risk,
        status=ApprovalStatus.PENDING,
        payload_fingerprint=approval_key(action, payload),
    )
    db.add(gate)
    db.flush()

    approval_gates_total.labels(action=action, status="requested").inc()
    pending_approvals.inc()
    audit.agent(
        "gate.opened",
        run_id=run_id,
        job_id=job_id,
        resource_type="approval_request",
        resource_id=gate.id,
        detail={"action": action, "risk": risk, "summary": summary, "payload": payload},
    )
    log.info("gate_opened", gate_id=gate.id, action=action, run_id=run_id, risk=risk)
    return gate


def decide_gate(
    db: Session,
    audit: AuditLogger,
    *,
    gate: ApprovalRequest,
    user: User,
    decision: str,
    note: str | None = None,
) -> ApprovalRequest:
    """Rule on a gate. Enforces I1, I2 and I3."""
    if user.role not in DECIDING_ROLES:  # I2
        # Independent: the caller turns this into a 403, which rolls the request
        # back. An attempt to authorise beyond one's role is exactly the kind of
        # event that must survive that rollback.
        audit.refusal(
            "gate.decision_forbidden",
            ActorType.HUMAN,
            actor_id=user.id,
            job_id=gate.job_id,
            run_id=gate.run_id,
            resource_type="approval_request",
            resource_id=gate.id,
            detail={"role": user.role.value, "attempted": decision},
        )
        raise GateForbidden(
            f"Role {user.role.value!r} may not decide approval gates; "
            f"requires one of {sorted(r.value for r in DECIDING_ROLES)}."
        )

    if gate.status is not ApprovalStatus.PENDING:  # I1
        raise GateError(
            f"Gate {gate.id} is already {gate.status.value}; a decided gate cannot be "
            "re-decided or replayed."
        )

    if decision not in ("approved", "rejected"):
        raise GateError(f"Unknown decision {decision!r}.")

    now = datetime.now(UTC)
    created = _aware(gate.created_at) or now
    waited = (now - created).total_seconds()

    gate.status = ApprovalStatus.APPROVED if decision == "approved" else ApprovalStatus.REJECTED
    gate.decided_by_id = user.id
    gate.decided_at = now
    gate.decision_note = note
    db.flush()

    approval_gates_total.labels(action=gate.action, status=decision).inc()
    approval_wait_seconds.labels(action=gate.action).observe(waited)
    pending_approvals.dec()

    audit.human(  # I3
        f"gate.{decision}",
        user_id=user.id,
        job_id=gate.job_id,
        run_id=gate.run_id,
        resource_type="approval_request",
        resource_id=gate.id,
        duration_ms=waited * 1000,
        detail={
            "action": gate.action,
            "risk": gate.risk,
            "note": note,
            "waited_seconds": round(waited, 2),
            "decider_role": user.role.value,
        },
    )
    log.info(
        "gate_decided",
        gate_id=gate.id,
        decision=decision,
        user_id=user.id,
        waited_s=round(waited, 2),
    )
    return gate


def expire_stale_gates(db: Session, audit: AuditLogger, ttl: timedelta = DEFAULT_TTL) -> int:
    """Close out gates nobody ruled on. Prevents an approval from being granted
    weeks later against a payload whose context is long gone."""
    cutoff = datetime.now(UTC) - ttl
    stale = (
        db.execute(select(ApprovalRequest).where(ApprovalRequest.status == ApprovalStatus.PENDING))
        .scalars()
        .all()
    )

    expired = 0
    for gate in stale:
        created = _aware(gate.created_at)
        if created is None or created > cutoff:
            continue
        gate.status = ApprovalStatus.EXPIRED
        gate.decided_at = datetime.now(UTC)
        pending_approvals.dec()
        audit.system(
            "gate.expired",
            job_id=gate.job_id,
            run_id=gate.run_id,
            resource_type="approval_request",
            resource_id=gate.id,
            detail={"action": gate.action, "ttl_hours": ttl.total_seconds() / 3600},
        )
        expired += 1

    if expired:
        db.flush()
    return expired


def granted_keys_for_run(db: Session, run_id: str, audit: AuditLogger | None = None) -> set[str]:
    """Fingerprints a human has approved for this run (I4, I5).

    The orchestrator loads these into `ToolContext.granted_approvals`. Two things
    make the binding real:

      * the key comes from `payload_fingerprint`, stamped when the gate was opened,
        not from the live `payload` column. Recomputing it here would compare the
        tampered payload against itself and always match;
      * a gate whose payload no longer hashes to its stored fingerprint has been
        altered after the human ruled on it. Its grant is dropped and the tampering
        is audited, so the gated tool stops rather than acting on the wider scope.
    """
    rows = (
        db.execute(
            select(ApprovalRequest).where(
                ApprovalRequest.run_id == run_id,
                ApprovalRequest.status == ApprovalStatus.APPROVED,
            )
        )
        .scalars()
        .all()
    )

    granted: set[str] = set()
    for g in rows:
        current = approval_key(g.action, g.payload)
        expected = g.payload_fingerprint or current  # pre-migration rows
        if current != expected:
            log.error(
                "approval_payload_tampered",
                gate_id=g.id,
                run_id=run_id,
                action=g.action,
                expected=expected,
                actual=current,
            )
            if audit is not None:
                audit.system(
                    "security.approval_payload_tampered",
                    job_id=g.job_id,
                    run_id=run_id,
                    outcome="denied",
                    resource_type="approval_request",
                    resource_id=g.id,
                    detail={
                        "action": g.action,
                        "approved_fingerprint": expected,
                        "current_fingerprint": current,
                        "note": "grant revoked; the gated action will not run",
                    },
                )
            continue
        granted.add(expected)
    return granted


def pending_for_job(db: Session, job_id: str) -> list[ApprovalRequest]:
    return list(
        db.execute(
            select(ApprovalRequest)
            .where(
                ApprovalRequest.job_id == job_id,
                ApprovalRequest.status == ApprovalStatus.PENDING,
            )
            .order_by(ApprovalRequest.created_at)
        ).scalars()
    )


def recount_pending(db: Session) -> int:
    """Re-sync the pending_approvals gauge after a restart."""
    count = (
        db.query(ApprovalRequest).filter(ApprovalRequest.status == ApprovalStatus.PENDING).count()
    )
    pending_approvals.set(count)
    return count
