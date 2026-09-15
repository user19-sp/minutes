"""Contracts between the platform and the ML services.

THIS FILE IS THE INTEGRATION SEAM WITH PERSON A.

The platform never imports torch, whisper or pyannote. It depends only on these
Protocols. Person A supplies concrete implementations; the platform binds them in
`backend/app/ml/registry.py` via configuration. Consequences:

  * the API, governance layer and UI are testable without a GPU or model weights;
  * swapping faster-whisper for Whisper changes one binding, not the pipeline;
  * a stub and a real model are interchangeable in the agent-vs-no-agent study.

Every implementation MUST populate `model_name` and `confidence`. The governance
layer routes low-confidence output to a stricter human gate, and the audit log
records which model produced each item -- unsourced output is a grading failure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable


class MLServiceError(Exception):
    """Raised by any ML service on unrecoverable failure.

    Implementations should raise this rather than a library-specific exception so
    the orchestrator can classify the failure without importing ML libraries.
    """


# --------------------------------------------------------------------------- #
# Result types
# --------------------------------------------------------------------------- #


@dataclass
class Segment:
    """One utterance. `speaker` is None until diarization runs."""

    start: float
    end: float
    text: str
    speaker: str | None = None

    def to_dict(self) -> dict:
        return {"start": self.start, "end": self.end, "text": self.text, "speaker": self.speaker}


@dataclass
class TranscriptionResult:
    text: str
    segments: list[Segment] = field(default_factory=list)
    detected_languages: list[str] = field(default_factory=list)
    is_code_mixed: bool = False
    duration_seconds: float | None = None
    model_name: str = "unknown"


@dataclass
class DiarizationResult:
    segments: list[Segment]
    speaker_count: int
    model_name: str = "unknown"


@dataclass
class AgendaBlockResult:
    position: int
    title: str
    text: str
    start_char: int
    end_char: int
    confidence: float = 0.0


@dataclass
class SegmentationResult:
    blocks: list[AgendaBlockResult]
    model_name: str = "unknown"


@dataclass
class ExtractedDecision:
    text: str
    confidence: float
    evidence_quote: str | None = None
    agenda_position: int | None = None


@dataclass
class ExtractedAction:
    text: str
    owner_name: str | None
    deadline: str | None
    confidence: float
    evidence_quote: str | None = None
    agenda_position: int | None = None


@dataclass
class ExtractionResult:
    decisions: list[ExtractedDecision] = field(default_factory=list)
    actions: list[ExtractedAction] = field(default_factory=list)
    model_name: str = "unknown"


# --------------------------------------------------------------------------- #
# Protocols -- Person A implements these
# --------------------------------------------------------------------------- #


@runtime_checkable
class SpeechToText(Protocol):
    name: str

    def transcribe(
        self, audio_path: Path, language_hint: str | None = None
    ) -> TranscriptionResult: ...


@runtime_checkable
class Diarizer(Protocol):
    name: str

    def diarize(self, audio_path: Path, segments: list[Segment]) -> DiarizationResult: ...


@runtime_checkable
class AgendaSegmenter(Protocol):
    name: str

    def segment(self, text: str, segments: list[Segment] | None = None) -> SegmentationResult: ...


@runtime_checkable
class DecisionActionExtractor(Protocol):
    name: str

    def extract(self, blocks: list[AgendaBlockResult]) -> ExtractionResult: ...
