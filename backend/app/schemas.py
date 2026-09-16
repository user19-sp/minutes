"""Pydantic API/data contracts.

These are the documented interface between the frontend, the platform and
Person A's ML services. Changing a field here is a contract change -- it must be
reflected in docs/api-contracts.md and the OpenAPI schema at /docs.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from backend.app.models import (
    ActorType,
    ApprovalStatus,
    ItemStatus,
    JobStatus,
    Role,
    RunMode,
    RunStatus,
)

ORM = ConfigDict(from_attributes=True)


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #


class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=10, max_length=128)
    full_name: str | None = Field(default=None, max_length=255)
    role: Role = Role.REVIEWER


class UserOut(BaseModel):
    model_config = ORM
    id: str
    email: str
    full_name: str | None
    role: Role
    is_active: bool
    created_at: datetime


class ApprovedEmailCreate(BaseModel):
    email: EmailStr
    note: str | None = Field(default=None, max_length=255)


class ApprovedEmailOut(BaseModel):
    model_config = ORM
    id: str
    email: str
    note: str | None
    added_by_id: str | None
    added_at: datetime
    used_at: datetime | None


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int
    user: UserOut


# --------------------------------------------------------------------------- #
# Jobs / transcripts
# --------------------------------------------------------------------------- #


class JobOut(BaseModel):
    model_config = ORM
    id: str
    owner_id: str
    original_filename: str
    content_type: str
    size_bytes: int
    sha256: str
    title: str | None
    language_hint: str | None
    status: JobStatus
    error_message: str | None
    created_at: datetime
    updated_at: datetime


class TranscriptSegment(BaseModel):
    """One diarized utterance. `speaker` is None when diarization is off."""

    start: float
    end: float
    speaker: str | None = None
    text: str


class TranscriptOut(BaseModel):
    model_config = ORM
    id: str
    job_id: str
    text: str
    detected_languages: list[str]
    is_code_mixed: bool
    duration_seconds: float | None
    model_name: str
    segments: list[TranscriptSegment]
    diarization_enabled: bool
    pii_redaction_count: int
    created_at: datetime


class AgendaBlockOut(BaseModel):
    model_config = ORM
    id: str
    position: int
    title: str
    text: str
    start_char: int
    end_char: int
    confidence: float


# --------------------------------------------------------------------------- #
# Minutes (decisions + actions)
# --------------------------------------------------------------------------- #


class DecisionOut(BaseModel):
    model_config = ORM
    id: str
    job_id: str
    agenda_block_id: str | None
    text: str
    confidence: float
    status: ItemStatus
    source_tool: str
    evidence_quote: str | None
    run_id: str | None
    original_text: str | None
    reviewed_by_id: str | None
    reviewed_at: datetime | None
    review_note: str | None
    created_at: datetime


class ActionItemOut(BaseModel):
    model_config = ORM
    id: str
    job_id: str
    agenda_block_id: str | None
    decision_id: str | None
    text: str
    owner_name: str | None
    deadline: str | None
    confidence: float
    status: ItemStatus
    source_tool: str
    evidence_quote: str | None
    run_id: str | None
    original_text: str | None
    original_owner_name: str | None
    original_deadline: str | None
    reviewed_by_id: str | None
    reviewed_at: datetime | None
    review_note: str | None
    review_seconds: float | None
    created_at: datetime


class MinutesOut(BaseModel):
    """Everything the reviewer editor needs for one job, in one round trip."""

    job: JobOut
    transcript: TranscriptOut | None
    agenda_blocks: list[AgendaBlockOut]
    decisions: list[DecisionOut]
    action_items: list[ActionItemOut]
    pending_approvals: list[ApprovalOut]
    latest_run: AgentRunOut | None


class ReviewAction(BaseModel):
    """A reviewer's ruling on one extracted item.

    `review_seconds` is supplied by the UI and feeds the reviewer-correction-time
    metric in the evaluation dossier.
    """

    status: Literal["approved", "rejected", "edited"]
    text: str | None = Field(default=None, max_length=4000)
    owner_name: str | None = Field(default=None, max_length=255)
    deadline: str | None = Field(default=None, max_length=64)
    note: str | None = Field(default=None, max_length=2000)
    review_seconds: float | None = Field(default=None, ge=0, le=86_400)

    @field_validator("text")
    @classmethod
    def _text_required_when_edited(cls, v: str | None, info: Any) -> str | None:
        if info.data.get("status") == "edited" and not (v or "").strip():
            raise ValueError("text is required when status is 'edited'")
        return v


# --------------------------------------------------------------------------- #
# Governance
# --------------------------------------------------------------------------- #


class ToolSpecOut(BaseModel):
    """A tool the orchestrator is permitted to call. Rendered on the governance page
    so a reviewer can see exactly what the agent can and cannot do."""

    name: str
    description: str
    side_effect: Literal["read", "write", "external"]
    requires_approval: bool
    max_calls_per_run: int
    input_schema: dict[str, Any]


class RunStartRequest(BaseModel):
    mode: RunMode = RunMode.AGENT
    enable_diarization: bool = False


class AgentRunOut(BaseModel):
    model_config = ORM
    id: str
    job_id: str
    mode: RunMode
    status: RunStatus
    trace_id: str
    tool_call_count: int
    denied_tool_call_count: int
    plan: list[dict[str, Any]]
    error_message: str | None
    started_at: datetime
    finished_at: datetime | None
    duration_ms: float | None


class ApprovalOut(BaseModel):
    model_config = ORM
    id: str
    job_id: str
    run_id: str
    action: str
    summary: str
    payload: dict[str, Any]
    risk: str
    status: ApprovalStatus
    decided_by_id: str | None
    decided_at: datetime | None
    decision_note: str | None
    created_at: datetime


class ApprovalDecision(BaseModel):
    decision: Literal["approved", "rejected"]
    note: str | None = Field(default=None, max_length=2000)


class AuditEventOut(BaseModel):
    model_config = ORM
    id: str
    created_at: datetime
    actor_type: ActorType
    actor_id: str | None
    action: str
    outcome: str
    resource_type: str | None
    resource_id: str | None
    job_id: str | None
    run_id: str | None
    trace_id: str | None
    detail: dict[str, Any]
    duration_ms: float | None


# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #


class ExportRequest(BaseModel):
    format: Literal["csv", "json", "tracker"] = "csv"
    # Guard-rail: exporting un-reviewed items is opt-in and always audited.
    include_unapproved: bool = False


class ExportOut(BaseModel):
    model_config = ORM
    id: str
    job_id: str
    run_id: str | None
    approval_id: str | None
    format: str
    stored_path: str
    item_count: int
    sha256: str
    created_by_id: str | None
    created_at: datetime


# --------------------------------------------------------------------------- #
# Misc
# --------------------------------------------------------------------------- #


class HealthOut(BaseModel):
    status: Literal["ok", "degraded"]
    version: str
    environment: str
    database: str


class MessageOut(BaseModel):
    detail: str


MinutesOut.model_rebuild()
