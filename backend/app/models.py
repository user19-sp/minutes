"""SQLAlchemy ORM models -- the persistent data contract for the platform.

Tables group into four concerns:
  * identity       : users
  * pipeline data  : jobs, transcripts, agenda_blocks, decisions, action_items
  * governance     : agent_runs, approval_requests, audit_events
  * outputs        : exports
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.db import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #


class Role(enum.StrEnum):
    """RBAC roles. `reviewer` and `admin` are the only roles permitted to decide gates."""

    VIEWER = "viewer"
    REVIEWER = "reviewer"
    ADMIN = "admin"


class JobStatus(enum.StrEnum):
    UPLOADED = "uploaded"
    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    READY_FOR_REVIEW = "ready_for_review"
    COMPLETED = "completed"
    FAILED = "failed"


class ItemStatus(enum.StrEnum):
    """Lifecycle of an extracted decision or action item."""

    PROPOSED = "proposed"
    APPROVED = "approved"
    EDITED = "edited"
    REJECTED = "rejected"


class RunMode(enum.StrEnum):
    """Distinguishes the two arms of the agent-vs-no-agent comparison study."""

    AGENT = "agent"
    NO_AGENT = "no_agent"


class RunStatus(enum.StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    ABORTED = "aborted"


class ApprovalStatus(enum.StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class ActorType(enum.StrEnum):
    HUMAN = "human"
    AGENT = "agent"
    SYSTEM = "system"


# --------------------------------------------------------------------------- #
# Identity
# --------------------------------------------------------------------------- #


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(255))
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[Role] = mapped_column(Enum(Role), default=Role.REVIEWER, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    jobs: Mapped[list[Job]] = relationship(back_populates="owner")


class ApprovedEmail(Base):
    """An address an administrator has cleared to register.

    The threat model assumes a single-tenant internal deployment with no public
    registration. This table is what enforces that: without an entry here,
    `POST /auth/register` refuses.

    An explicit address list rather than a domain rule, deliberately. A domain
    rule needs a company domain to demonstrate; a list can be administered live,
    which makes the control visible rather than asserted.
    """

    __tablename__ = "approved_emails"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    note: Mapped[str | None] = mapped_column(String(255))

    added_by_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    #: Stamped when someone actually registers with this address, so an admin can
    #: see which invitations are outstanding.
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# --------------------------------------------------------------------------- #
# Pipeline data
# --------------------------------------------------------------------------- #


class Job(Base):
    """One uploaded meeting recording and everything derived from it."""

    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)

    original_filename: Mapped[str] = mapped_column(String(512), nullable=False)
    stored_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    content_type: Mapped[str] = mapped_column(String(128), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), index=True, nullable=False)

    title: Mapped[str | None] = mapped_column(String(512))
    language_hint: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus), default=JobStatus.UPLOADED, nullable=False, index=True
    )
    error_message: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    owner: Mapped[User] = relationship(back_populates="jobs")
    transcript: Mapped[Transcript | None] = relationship(
        back_populates="job", cascade="all, delete-orphan", uselist=False
    )
    decisions: Mapped[list[Decision]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )
    action_items: Mapped[list[ActionItem]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )
    runs: Mapped[list[AgentRun]] = relationship(back_populates="job", cascade="all, delete-orphan")


class Transcript(Base):
    """STT output. Text is PII-scrubbed before it is ever persisted."""

    __tablename__ = "transcripts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    job_id: Mapped[str] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), unique=True, index=True
    )

    text: Mapped[str] = mapped_column(Text, nullable=False)
    detected_languages: Mapped[list] = mapped_column(JSON, default=list)
    is_code_mixed: Mapped[bool] = mapped_column(Boolean, default=False)
    duration_seconds: Mapped[float | None] = mapped_column(Float)
    model_name: Mapped[str] = mapped_column(String(128), default="unknown")
    # [{start, end, speaker, text}] -- diarized when Person A's module is enabled
    segments: Mapped[list] = mapped_column(JSON, default=list)
    diarization_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    pii_redaction_count: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    job: Mapped[Job] = relationship(back_populates="transcript")
    agenda_blocks: Mapped[list[AgendaBlock]] = relationship(
        back_populates="transcript", cascade="all, delete-orphan", order_by="AgendaBlock.position"
    )


class AgendaBlock(Base):
    """A topic-coherent slice of the transcript."""

    __tablename__ = "agenda_blocks"
    __table_args__ = (UniqueConstraint("transcript_id", "position", name="uq_block_position"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    transcript_id: Mapped[str] = mapped_column(
        ForeignKey("transcripts.id", ondelete="CASCADE"), index=True
    )

    position: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    start_char: Mapped[int] = mapped_column(Integer, default=0)
    end_char: Mapped[int] = mapped_column(Integer, default=0)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)

    transcript: Mapped[Transcript] = relationship(back_populates="agenda_blocks")


class Decision(Base):
    """A decision the meeting reached: proposed by the extractor, ruled on by a human."""

    __tablename__ = "decisions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    agenda_block_id: Mapped[str | None] = mapped_column(
        ForeignKey("agenda_blocks.id", ondelete="SET NULL")
    )

    text: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[ItemStatus] = mapped_column(
        Enum(ItemStatus), default=ItemStatus.PROPOSED, nullable=False, index=True
    )

    # Traceability: which extractor produced this, and the verbatim span it came from.
    source_tool: Mapped[str] = mapped_column(String(128), default="unknown")
    evidence_quote: Mapped[str | None] = mapped_column(Text)
    run_id: Mapped[str | None] = mapped_column(String(36), index=True)

    # Human review trail
    original_text: Mapped[str | None] = mapped_column(Text)
    reviewed_by_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_note: Mapped[str | None] = mapped_column(Text)
    review_seconds: Mapped[float | None] = mapped_column(Float)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    job: Mapped[Job] = relationship(back_populates="decisions")
    action_items: Mapped[list[ActionItem]] = relationship(back_populates="decision")


class ActionItem(Base):
    """A task with an owner and a deadline. The unit that gets exported."""

    __tablename__ = "action_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    agenda_block_id: Mapped[str | None] = mapped_column(
        ForeignKey("agenda_blocks.id", ondelete="SET NULL")
    )
    decision_id: Mapped[str | None] = mapped_column(ForeignKey("decisions.id", ondelete="SET NULL"))

    text: Mapped[str] = mapped_column(Text, nullable=False)
    owner_name: Mapped[str | None] = mapped_column(String(255))
    deadline: Mapped[str | None] = mapped_column(String(64))  # ISO date, or free text as spoken
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[ItemStatus] = mapped_column(
        Enum(ItemStatus), default=ItemStatus.PROPOSED, nullable=False, index=True
    )

    source_tool: Mapped[str] = mapped_column(String(128), default="unknown")
    evidence_quote: Mapped[str | None] = mapped_column(Text)
    run_id: Mapped[str | None] = mapped_column(String(36), index=True)

    original_text: Mapped[str | None] = mapped_column(Text)
    original_owner_name: Mapped[str | None] = mapped_column(String(255))
    original_deadline: Mapped[str | None] = mapped_column(String(64))
    reviewed_by_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_note: Mapped[str | None] = mapped_column(Text)
    # Wall-clock seconds the reviewer spent on this item (correction-time metric).
    review_seconds: Mapped[float | None] = mapped_column(Float)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    job: Mapped[Job] = relationship(back_populates="action_items")
    decision: Mapped[Decision | None] = relationship(back_populates="action_items")


# --------------------------------------------------------------------------- #
# Governance
# --------------------------------------------------------------------------- #


class AgentRun(Base):
    """One orchestrator execution over one job."""

    __tablename__ = "agent_runs"
    # The worker polls (status, queued_at) on every tick. Declared here as well as
    # in the migration so autogenerate does not keep proposing to drop it.
    __table_args__ = (Index("ix_agent_runs_status_queued_at", "status", "queued_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    mode: Mapped[RunMode] = mapped_column(Enum(RunMode), default=RunMode.AGENT, nullable=False)
    status: Mapped[RunStatus] = mapped_column(
        Enum(RunStatus), default=RunStatus.RUNNING, nullable=False, index=True
    )

    trace_id: Mapped[str] = mapped_column(String(36), index=True, default=_uuid)
    tool_call_count: Mapped[int] = mapped_column(Integer, default=0)
    denied_tool_call_count: Mapped[int] = mapped_column(Integer, default=0)
    plan: Mapped[list] = mapped_column(JSON, default=list)
    error_message: Mapped[str | None] = mapped_column(Text)

    # --- queue bookkeeping ------------------------------------------------ #
    # The run row IS the queue entry. A separate queue table would be a second
    # source of truth about the same work, and the two would drift.
    enable_diarization: Mapped[bool] = mapped_column(Boolean, default=False)
    #: Set when a worker takes the run; used to detect abandoned work.
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claimed_by: Mapped[str | None] = mapped_column(String(128))
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    queued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[float | None] = mapped_column(Float)

    job: Mapped[Job] = relationship(back_populates="runs")
    approvals: Mapped[list[ApprovalRequest]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class ApprovalRequest(Base):
    """A write/export action the agent proposed and may not perform until a human rules on it.

    The orchestrator creates these; only a `reviewer` or `admin` may decide them.
    """

    __tablename__ = "approval_requests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id", ondelete="CASCADE"), index=True)

    action: Mapped[str] = mapped_column(String(128), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    risk: Mapped[str] = mapped_column(String(32), default="medium")

    #: Fingerprint of `payload` computed when the gate was OPENED, before any human
    #: saw it. The grant is checked against this, never against a value recomputed
    #: from the live row -- otherwise editing `payload` would move both sides of the
    #: comparison together and the binding would be vacuous.
    payload_fingerprint: Mapped[str] = mapped_column(String(80), default="", nullable=False)

    status: Mapped[ApprovalStatus] = mapped_column(
        Enum(ApprovalStatus), default=ApprovalStatus.PENDING, nullable=False, index=True
    )
    decided_by_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_note: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    run: Mapped[AgentRun] = relationship(back_populates="approvals")


class AuditEvent(Base):
    """Append-only trail. Every orchestrator step, tool call (allowed *and* denied),
    human decision and export lands here. Nothing in the app updates or deletes rows.
    """

    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_job_ts", "job_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )

    actor_type: Mapped[ActorType] = mapped_column(Enum(ActorType), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    outcome: Mapped[str] = mapped_column(String(32), default="success")

    resource_type: Mapped[str | None] = mapped_column(String(64))
    resource_id: Mapped[str | None] = mapped_column(String(36))
    job_id: Mapped[str | None] = mapped_column(String(36), index=True)
    run_id: Mapped[str | None] = mapped_column(String(36), index=True)
    trace_id: Mapped[str | None] = mapped_column(String(36), index=True)

    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    duration_ms: Mapped[float | None] = mapped_column(Float)


# --------------------------------------------------------------------------- #
# Outputs
# --------------------------------------------------------------------------- #


class ExportRecord(Base):
    """Provenance for every file that left the system."""

    __tablename__ = "exports"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    run_id: Mapped[str | None] = mapped_column(String(36))
    approval_id: Mapped[str | None] = mapped_column(String(36))

    format: Mapped[str] = mapped_column(String(16), nullable=False)
    stored_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    item_count: Mapped[int] = mapped_column(Integer, default=0)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)

    created_by_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
