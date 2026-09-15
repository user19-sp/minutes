"""Append-only audit trail.

Every consequential event -- orchestrator step, tool call (allowed *and* denied),
human ruling, export -- is written here. Rules:

  * append only: nothing in the application updates or deletes an audit row;
  * always attributed: actor_type and actor_id are mandatory;
  * always correlated: trace_id ties a row to the HTTP request and the log line;
  * never sensitive: payloads are size-capped and secret keys are stripped.

An action that is not audited is an action the system cannot account for, so the
writer is deliberately hard to bypass -- every tool call goes through it.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.models import ActorType, AuditEvent
from backend.app.observability.logging import get_logger, trace_id_ctx

log = get_logger("audit")

# Cap a single detail payload so a huge transcript cannot bloat the audit table.
MAX_DETAIL_CHARS = 4_000

SENSITIVE_KEYS = {
    "password",
    "hashed_password",
    "token",
    "access_token",
    "authorization",
    "secret",
    "api_key",
    "jwt_secret",
}


def _sanitise(value: Any, depth: int = 0) -> Any:
    """Strip secrets and truncate oversized values before they reach the table."""
    if depth > 4:
        return "[TRUNCATED_DEPTH]"
    if isinstance(value, dict):
        return {
            k: ("[REDACTED]" if k.lower() in SENSITIVE_KEYS else _sanitise(v, depth + 1))
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return [_sanitise(v, depth + 1) for v in value[:50]]
    if isinstance(value, str) and len(value) > MAX_DETAIL_CHARS:
        return value[:MAX_DETAIL_CHARS] + f"...[+{len(value) - MAX_DETAIL_CHARS} chars]"
    return value


class AuditLogger:
    """Writes audit rows on a caller-supplied session.

    Two persistence modes, because success and refusal need opposite guarantees:

    * **Transactional** (default). The row commits with the caller's transaction,
      so an audited action and its evidence succeed or fail together. A tool call
      that rolls back leaves no misleading "it happened" row.

    * **Independent** (`independent=True`). The row is written on its own session
      and committed immediately. Refusal paths need this: they record the event
      and then raise an HTTPException, which makes the request handler roll back.
      Under the transactional mode the evidence of the refusal would be destroyed
      along with the operation it refused -- the exact opposite of what the audit
      trail is for. A rejected login, a blocked brute-force attempt and a refused
      upload all *happened*, and must be recorded even though nothing else was.
    """

    def __init__(self, db: Session) -> None:
        self.db = db

    def record(
        self,
        *,
        action: str,
        actor_type: ActorType,
        actor_id: str | None = None,
        outcome: str = "success",
        resource_type: str | None = None,
        resource_id: str | None = None,
        job_id: str | None = None,
        run_id: str | None = None,
        detail: dict[str, Any] | None = None,
        duration_ms: float | None = None,
        independent: bool = False,
    ) -> AuditEvent:
        if independent:
            return self._record_independent(
                action=action,
                actor_type=actor_type,
                actor_id=actor_id,
                outcome=outcome,
                resource_type=resource_type,
                resource_id=resource_id,
                job_id=job_id,
                run_id=run_id,
                detail=detail,
                duration_ms=duration_ms,
            )

        event = AuditEvent(
            action=action,
            actor_type=actor_type,
            actor_id=actor_id,
            outcome=outcome,
            resource_type=resource_type,
            resource_id=resource_id,
            job_id=job_id,
            run_id=run_id,
            trace_id=trace_id_ctx.get(),
            detail=_sanitise(detail or {}),
            duration_ms=duration_ms,
        )
        self.db.add(event)
        self.db.flush()

        log.info(
            "audit",
            audit_action=action,
            actor_type=actor_type.value,
            actor_id=actor_id,
            outcome=outcome,
            resource_type=resource_type,
            resource_id=resource_id,
            job_id=job_id,
            run_id=run_id,
        )
        return event

    def _record_independent(self, **kwargs: Any) -> AuditEvent:
        """Persist on a fresh session so the row survives the caller's rollback."""
        from backend.app.db import SessionLocal

        detail = kwargs.pop("detail", None)
        with SessionLocal() as session:
            event = AuditEvent(
                trace_id=trace_id_ctx.get(),
                detail=_sanitise(detail or {}),
                **kwargs,
            )
            session.add(event)
            session.commit()
            session.refresh(event)

        log.info(
            "audit",
            audit_action=kwargs.get("action"),
            actor_type=kwargs["actor_type"].value,
            actor_id=kwargs.get("actor_id"),
            outcome=kwargs.get("outcome", "success"),
            independent=True,
        )
        return event

    def refusal(self, action: str, actor_type: ActorType, **kwargs: Any) -> AuditEvent:
        """Record a refusal. Always independent -- see the class docstring."""
        kwargs.setdefault("outcome", "denied")
        return self.record(action=action, actor_type=actor_type, independent=True, **kwargs)

    # Convenience wrappers -------------------------------------------------- #

    def human(self, action: str, user_id: str, **kwargs: Any) -> AuditEvent:
        return self.record(action=action, actor_type=ActorType.HUMAN, actor_id=user_id, **kwargs)

    def agent(self, action: str, run_id: str, **kwargs: Any) -> AuditEvent:
        kwargs.pop("actor_id", None)
        return self.record(
            action=action,
            actor_type=ActorType.AGENT,
            actor_id=f"orchestrator:{run_id}",
            run_id=run_id,
            **kwargs,
        )

    def system(self, action: str, **kwargs: Any) -> AuditEvent:
        return self.record(action=action, actor_type=ActorType.SYSTEM, actor_id="system", **kwargs)


def get_trail(
    db: Session,
    *,
    job_id: str | None = None,
    run_id: str | None = None,
    action: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> list[AuditEvent]:
    """Read the trail, newest first. Read-only -- there is no update or delete path."""
    stmt = select(AuditEvent)
    if job_id:
        stmt = stmt.where(AuditEvent.job_id == job_id)
    if run_id:
        stmt = stmt.where(AuditEvent.run_id == run_id)
    if action:
        stmt = stmt.where(AuditEvent.action == action)
    stmt = stmt.order_by(AuditEvent.created_at.desc()).limit(limit).offset(offset)
    return list(db.execute(stmt).scalars())


def export_trail_jsonl(events: list[AuditEvent]) -> str:
    """Serialise a trail for the evidence appendix of the final report."""
    lines = []
    for e in events:
        lines.append(
            json.dumps(
                {
                    "id": e.id,
                    "ts": e.created_at.isoformat() if e.created_at else None,
                    "actor_type": e.actor_type.value,
                    "actor_id": e.actor_id,
                    "action": e.action,
                    "outcome": e.outcome,
                    "resource": {"type": e.resource_type, "id": e.resource_id},
                    "job_id": e.job_id,
                    "run_id": e.run_id,
                    "trace_id": e.trace_id,
                    "duration_ms": e.duration_ms,
                    "detail": e.detail,
                },
                ensure_ascii=False,
            )
        )
    return "\n".join(lines)
