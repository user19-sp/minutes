"""Task export -- the last stage of the pipeline and the highest-risk one.

Export is the only path by which data leaves the system, so it is the strictest:

  * it is an `external` side-effect tool, therefore always behind a human gate;
  * by default it refuses to emit anything a reviewer has not approved;
  * the CSV writer defends against formula injection (a cell beginning `=`, `+`,
    `-` or `@` executes when the file is opened in Excel -- an action item reading
    `=cmd|'/c calc'!A1` would otherwise run on the recipient's machine);
  * every file produced is hashed and recorded in `exports` for provenance.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from backend.app.config import settings
from backend.app.models import ActionItem, Decision, ItemStatus, Job

EXPORTABLE_STATUSES = {ItemStatus.APPROVED, ItemStatus.EDITED}

CSV_COLUMNS = [
    "action_id",
    "job_id",
    "meeting_title",
    "task",
    "owner",
    "deadline",
    "status",
    "confidence",
    "source_tool",
    "agenda_item",
    "evidence_quote",
    "reviewed_at",
]

#: Characters Excel/Sheets treat as the start of a formula.
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


class ExportError(Exception):
    """Export could not be produced."""


@dataclass
class ExportArtifact:
    path: Path
    item_count: int
    sha256: str
    format: str


def neutralise_csv(value: Any) -> str:
    """Prefix a leading formula character with `'` so spreadsheets treat it as text."""
    text = "" if value is None else str(value)
    if text.startswith(FORMULA_PREFIXES):
        return "'" + text
    return text


def selectable_actions(
    db: Session, job_id: str, include_unapproved: bool = False
) -> list[ActionItem]:
    """Which action items are eligible to leave the system."""
    query = db.query(ActionItem).filter(ActionItem.job_id == job_id)
    if not include_unapproved:
        query = query.filter(ActionItem.status.in_(EXPORTABLE_STATUSES))
    else:
        # Even in the opt-in path, an explicitly rejected item never exports.
        query = query.filter(ActionItem.status != ItemStatus.REJECTED)
    return query.order_by(ActionItem.created_at).all()


def _agenda_title(db: Session, block_id: str | None) -> str:
    if not block_id:
        return ""
    from backend.app.models import AgendaBlock

    block = db.get(AgendaBlock, block_id)
    return block.title if block else ""


def _rows(db: Session, job: Job, actions: Iterable[ActionItem]) -> list[dict[str, Any]]:
    return [
        {
            "action_id": a.id,
            "job_id": a.job_id,
            "meeting_title": job.title or job.original_filename,
            "task": a.text,
            "owner": a.owner_name or "",
            "deadline": a.deadline or "",
            "status": a.status.value,
            "confidence": a.confidence,
            "source_tool": a.source_tool,
            "agenda_item": _agenda_title(db, a.agenda_block_id),
            "evidence_quote": a.evidence_quote or "",
            "reviewed_at": a.reviewed_at.isoformat() if a.reviewed_at else "",
        }
        for a in actions
    ]


def _write(content: bytes, job_id: str, suffix: str) -> tuple[Path, str]:
    settings.export_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = settings.export_dir / f"{job_id}_{stamp}_{uuid.uuid4().hex[:8]}.{suffix}"
    path.write_bytes(content)
    return path, hashlib.sha256(content).hexdigest()


def build_csv(db: Session, job: Job, actions: list[ActionItem]) -> ExportArtifact:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=CSV_COLUMNS, quoting=csv.QUOTE_ALL)
    writer.writeheader()
    for row in _rows(db, job, actions):
        writer.writerow({k: neutralise_csv(v) for k, v in row.items()})
    # utf-8-sig so Excel opens Devanagari and other non-ASCII correctly.
    path, digest = _write(buffer.getvalue().encode("utf-8-sig"), job.id, "csv")
    return ExportArtifact(path=path, item_count=len(actions), sha256=digest, format="csv")


def build_json(
    db: Session, job: Job, actions: list[ActionItem], decisions: list[Decision]
) -> ExportArtifact:
    document = {
        "schema_version": "1.0",
        "generated_at": datetime.now(UTC).isoformat(),
        "meeting": {
            "job_id": job.id,
            "title": job.title or job.original_filename,
            "source_file": job.original_filename,
            "source_sha256": job.sha256,
            "language_hint": job.language_hint,
        },
        "decisions": [
            {
                "id": d.id,
                "text": d.text,
                "status": d.status.value,
                "confidence": d.confidence,
                "source_tool": d.source_tool,
                "evidence_quote": d.evidence_quote,
                "reviewed_at": d.reviewed_at.isoformat() if d.reviewed_at else None,
            }
            for d in decisions
        ],
        "action_items": _rows(db, job, actions),
    }
    payload = json.dumps(document, indent=2, ensure_ascii=False).encode("utf-8")
    path, digest = _write(payload, job.id, "json")
    return ExportArtifact(path=path, item_count=len(actions), sha256=digest, format="json")


def build_tracker_payload(db: Session, job: Job, actions: list[ActionItem]) -> ExportArtifact:
    """Mock task-tracker payload (Jira/Linear-shaped), written to disk rather than
    POSTed. Sending it to a real tracker would be an outbound network side effect,
    which this project deliberately does not perform."""
    issues = [
        {
            "fields": {
                "summary": a.text[:255],
                "description": (
                    f"Source meeting: {job.title or job.original_filename}\n"
                    f"Evidence: {a.evidence_quote or 'n/a'}\n"
                    f"Extractor: {a.source_tool} (confidence {a.confidence})\n"
                    f"Reviewed: {a.reviewed_at.isoformat() if a.reviewed_at else 'not reviewed'}"
                ),
                "assignee": {"displayName": a.owner_name} if a.owner_name else None,
                "duedate": a.deadline,
                "labels": ["meeting-minutes", f"job-{job.id[:8]}"],
            },
            "externalId": a.id,
        }
        for a in actions
    ]
    payload = json.dumps(
        {"issueUpdates": issues, "_note": "mock tracker payload; not transmitted"},
        indent=2,
        ensure_ascii=False,
    ).encode("utf-8")
    path, digest = _write(payload, job.id, "tracker.json")
    return ExportArtifact(path=path, item_count=len(actions), sha256=digest, format="tracker")


def build_export(
    db: Session, job: Job, fmt: str, include_unapproved: bool = False
) -> ExportArtifact:
    actions = selectable_actions(db, job.id, include_unapproved)
    if not actions:
        raise ExportError(
            "Nothing to export: no approved action items for this meeting. "
            "Approve items in the reviewer editor first."
        )
    if fmt == "csv":
        return build_csv(db, job, actions)
    if fmt == "json":
        decisions = (
            db.query(Decision)
            .filter(Decision.job_id == job.id, Decision.status.in_(EXPORTABLE_STATUSES))
            .all()
        )
        return build_json(db, job, actions, decisions)
    if fmt == "tracker":
        return build_tracker_payload(db, job, actions)
    raise ExportError(f"Unsupported export format {fmt!r}.")
