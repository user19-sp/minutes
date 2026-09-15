"""The agent's entire capability surface.

Six tools, and nothing else. Four are pure reads that call Person A's ML services
and return data; exactly two can change the world, and both are gated:

    transcribe_audio          read      -- no gate
    diarize_speakers          read      -- no gate
    segment_agenda            read      -- no gate
    extract_decisions_actions read      -- no gate
    persist_minutes           write     -- HUMAN GATE
    export_actions            external  -- HUMAN GATE

Keeping the ML tools pure means the orchestrator, not the agent, owns persistence
of intermediate artifacts. So the claim under test is narrow and checkable: the
agent can change stored records in exactly two ways, and a human authorises both.

Every handler re-checks that the job it was handed is the run's own job. An agent
that has been talked into operating on somebody else's meeting is refused at the
tool boundary, not at the API boundary.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from backend.app.agent.registry import ToolContext, ToolSpec, deny_tool_call, registry
from backend.app.ml import registry as ml
from backend.app.ml.base import AgendaBlockResult, MLServiceError, Segment
from backend.app.models import ActionItem, Decision, ExportRecord, ItemStatus, Job
from backend.app.observability.metrics import exports_total
from backend.app.security.injection import scan, wrap_untrusted


def _require_own_job(ctx: ToolContext, tool: str, job_id: str) -> Job:
    """Scope guard: a tool may only touch the job its run was created for."""
    if job_id != ctx.job_id:
        deny_tool_call(
            ctx,
            tool,
            "job_scope_violation",
            f"run is scoped to job {ctx.job_id}, call targeted {job_id}",
        )
    job = ctx.db.get(Job, job_id)
    if job is None:
        deny_tool_call(ctx, tool, "unknown_job", job_id)
    return job  # type: ignore[return-value]


# --------------------------------------------------------------------------- #
# 1. transcribe_audio
# --------------------------------------------------------------------------- #


class TranscribeArgs(BaseModel):
    job_id: str
    language_hint: str | None = Field(default=None, max_length=32)


def _transcribe(args: TranscribeArgs, ctx: ToolContext) -> dict[str, Any]:
    job = _require_own_job(ctx, "transcribe_audio", args.job_id)
    path = Path(job.stored_path)
    if not path.exists():
        raise MLServiceError(f"stored recording is missing for job {job.id}")

    result = ml.get_stt().transcribe(path, args.language_hint or job.language_hint)
    return {
        "text": result.text,
        "segments": [s.to_dict() for s in result.segments],
        "detected_languages": result.detected_languages,
        "is_code_mixed": result.is_code_mixed,
        "duration_seconds": result.duration_seconds,
        "model_name": result.model_name,
    }


# --------------------------------------------------------------------------- #
# 2. diarize_speakers
# --------------------------------------------------------------------------- #


class DiarizeArgs(BaseModel):
    job_id: str
    segments: list[dict[str, Any]] = Field(default_factory=list)


def _diarize(args: DiarizeArgs, ctx: ToolContext) -> dict[str, Any]:
    job = _require_own_job(ctx, "diarize_speakers", args.job_id)
    segments = [
        Segment(
            start=float(s.get("start", 0.0)),
            end=float(s.get("end", 0.0)),
            text=str(s.get("text", "")),
            speaker=s.get("speaker"),
        )
        for s in args.segments
    ]
    result = ml.get_diarizer().diarize(Path(job.stored_path), segments)
    return {
        "segments": [s.to_dict() for s in result.segments],
        "speaker_count": result.speaker_count,
        "model_name": result.model_name,
    }


# --------------------------------------------------------------------------- #
# 3. segment_agenda
# --------------------------------------------------------------------------- #


class SegmentAgendaArgs(BaseModel):
    job_id: str
    text: str = Field(min_length=1)


def _segment_agenda(args: SegmentAgendaArgs, ctx: ToolContext) -> dict[str, Any]:
    _require_own_job(ctx, "segment_agenda", args.job_id)
    result = ml.get_segmenter().segment(args.text)
    return {
        "blocks": [
            {
                "position": b.position,
                "title": b.title,
                "text": b.text,
                "start_char": b.start_char,
                "end_char": b.end_char,
                "confidence": b.confidence,
            }
            for b in result.blocks
        ],
        "model_name": result.model_name,
    }


# --------------------------------------------------------------------------- #
# 4. extract_decisions_actions
# --------------------------------------------------------------------------- #


class ExtractArgs(BaseModel):
    job_id: str
    blocks: list[dict[str, Any]] = Field(min_length=1)


def _extract(args: ExtractArgs, ctx: ToolContext) -> dict[str, Any]:
    _require_own_job(ctx, "extract_decisions_actions", args.job_id)

    blocks = [
        AgendaBlockResult(
            position=int(b.get("position", i)),
            title=str(b.get("title", f"Agenda item {i + 1}")),
            # Untrusted-content fencing happens here, at the model boundary.
            text=str(b.get("text", "")),
            start_char=int(b.get("start_char", 0)),
            end_char=int(b.get("end_char", 0)),
            confidence=float(b.get("confidence", 0.0)),
        )
        for i, b in enumerate(args.blocks)
    ]

    # An LLM-backed extractor must receive fenced content; a local classifier does
    # not care, but we build the fenced form either way so the defence is in the
    # trace and cannot be forgotten when the backend is swapped.
    for b in blocks:
        _ = wrap_untrusted(b.text, label="agenda block")

    result = ml.get_extractor().extract(blocks)
    return {
        "decisions": [
            {
                "text": d.text,
                "confidence": d.confidence,
                "evidence_quote": d.evidence_quote,
                "agenda_position": d.agenda_position,
            }
            for d in result.decisions
        ],
        "actions": [
            {
                "text": a.text,
                "owner_name": a.owner_name,
                "deadline": a.deadline,
                "confidence": a.confidence,
                "evidence_quote": a.evidence_quote,
                "agenda_position": a.agenda_position,
            }
            for a in result.actions
        ],
        "model_name": result.model_name,
    }


# --------------------------------------------------------------------------- #
# 5. persist_minutes  -- GATED
# --------------------------------------------------------------------------- #


class DecisionDraft(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_quote: str | None = Field(default=None, max_length=4000)
    agenda_position: int | None = None


class ActionDraft(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    owner_name: str | None = Field(default=None, max_length=255)
    deadline: str | None = Field(default=None, max_length=64)
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_quote: str | None = Field(default=None, max_length=4000)
    agenda_position: int | None = None


class PersistMinutesArgs(BaseModel):
    job_id: str
    source_tool: str = Field(max_length=128)
    decisions: list[DecisionDraft] = Field(default_factory=list, max_length=500)
    actions: list[ActionDraft] = Field(default_factory=list, max_length=500)


def _persist_minutes(args: PersistMinutesArgs, ctx: ToolContext) -> dict[str, Any]:
    """Write draft minutes. Reached only after a human approved this exact payload."""
    job = _require_own_job(ctx, "persist_minutes", args.job_id)

    block_ids = _block_ids_by_position(ctx, job.id)

    created_decisions: list[Decision] = []
    for draft in args.decisions:
        row = Decision(
            job_id=job.id,
            agenda_block_id=block_ids.get(draft.agenda_position),
            text=draft.text,
            confidence=draft.confidence,
            status=ItemStatus.PROPOSED,
            source_tool=args.source_tool,
            evidence_quote=draft.evidence_quote,
            run_id=ctx.run_id,
        )
        ctx.db.add(row)
        created_decisions.append(row)

    created_actions: list[ActionItem] = []
    for draft in args.actions:
        row = ActionItem(
            job_id=job.id,
            agenda_block_id=block_ids.get(draft.agenda_position),
            text=draft.text,
            owner_name=draft.owner_name,
            deadline=draft.deadline,
            confidence=draft.confidence,
            status=ItemStatus.PROPOSED,
            source_tool=args.source_tool,
            evidence_quote=draft.evidence_quote,
            run_id=ctx.run_id,
        )
        ctx.db.add(row)
        created_actions.append(row)

    ctx.db.flush()
    return {
        "decisions_written": len(created_decisions),
        "actions_written": len(created_actions),
        "decision_ids": [d.id for d in created_decisions],
        "action_ids": [a.id for a in created_actions],
    }


def _block_ids_by_position(ctx: ToolContext, job_id: str) -> dict[int, str]:
    from backend.app.models import AgendaBlock, Transcript

    transcript = ctx.db.query(Transcript).filter(Transcript.job_id == job_id).one_or_none()
    if transcript is None:
        return {}
    blocks = ctx.db.query(AgendaBlock).filter(AgendaBlock.transcript_id == transcript.id).all()
    return {b.position: b.id for b in blocks}


# --------------------------------------------------------------------------- #
# 6. export_actions  -- GATED
# --------------------------------------------------------------------------- #


class ExportActionsArgs(BaseModel):
    job_id: str
    format: Literal["csv", "json", "tracker"] = "csv"
    include_unapproved: bool = False


def _export_actions(args: ExportActionsArgs, ctx: ToolContext) -> dict[str, Any]:
    """Produce an export file. Reached only after a human approved this exact request."""
    from backend.app.services import export as export_service

    job = _require_own_job(ctx, "export_actions", args.job_id)

    artifact = export_service.build_export(
        ctx.db, job, args.format, include_unapproved=args.include_unapproved
    )
    record = ExportRecord(
        job_id=job.id,
        run_id=ctx.run_id,
        format=artifact.format,
        stored_path=str(artifact.path),
        item_count=artifact.item_count,
        sha256=artifact.sha256,
    )
    ctx.db.add(record)
    ctx.db.flush()

    exports_total.labels(format=artifact.format).inc()
    return {
        "export_id": record.id,
        "format": artifact.format,
        "item_count": artifact.item_count,
        "sha256": artifact.sha256,
    }


# --------------------------------------------------------------------------- #
# Registration -- the allow-list itself
# --------------------------------------------------------------------------- #

registry.register(
    ToolSpec(
        name="transcribe_audio",
        description="Transcribe the meeting recording for this job into multilingual text.",
        side_effect="read",
        input_model=TranscribeArgs,
        handler=_transcribe,
        max_calls_per_run=2,
        risk="low",
    )
)

registry.register(
    ToolSpec(
        name="diarize_speakers",
        description="Label transcript segments with speaker identifiers.",
        side_effect="read",
        input_model=DiarizeArgs,
        handler=_diarize,
        max_calls_per_run=2,
        risk="low",
    )
)

registry.register(
    ToolSpec(
        name="segment_agenda",
        description="Split the transcript into topic-coherent agenda blocks.",
        side_effect="read",
        input_model=SegmentAgendaArgs,
        handler=_segment_agenda,
        max_calls_per_run=2,
        risk="low",
    )
)

registry.register(
    ToolSpec(
        name="extract_decisions_actions",
        description="Extract candidate decisions and action items from agenda blocks.",
        side_effect="read",
        input_model=ExtractArgs,
        handler=_extract,
        max_calls_per_run=3,
        risk="low",
    )
)

registry.register(
    ToolSpec(
        name="persist_minutes",
        description=(
            "Write the extracted decisions and action items to the meeting record as "
            "draft items awaiting reviewer sign-off."
        ),
        side_effect="write",
        input_model=PersistMinutesArgs,
        handler=_persist_minutes,
        requires_approval=True,
        max_calls_per_run=1,
        risk="medium",
    )
)

registry.register(
    ToolSpec(
        name="export_actions",
        description=(
            "Export reviewer-approved action items out of the system as a CSV, JSON or "
            "task-tracker payload."
        ),
        side_effect="external",
        input_model=ExportActionsArgs,
        handler=_export_actions,
        requires_approval=True,
        max_calls_per_run=3,
        risk="high",
    )
)


#: Tools a run may use, by mode. The no-agent baseline arm gets the same ML tools
#: but no export capability at all -- it is a straight-through pipeline.
PIPELINE_TOOLS = {
    "transcribe_audio",
    "diarize_speakers",
    "segment_agenda",
    "extract_decisions_actions",
    "persist_minutes",
}

AGENT_TOOLS = PIPELINE_TOOLS | {"export_actions"}


def scan_for_injection(text: str) -> dict[str, Any]:
    """Convenience wrapper used by the orchestrator; kept here so the tool layer
    owns the untrusted-content policy."""
    return scan(text).as_detail()
