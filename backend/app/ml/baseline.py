"""Rule/keyword baseline implementations of the ML contracts.

Two jobs at once:

  1. They let the whole platform run end-to-end today, with no model weights, no
     GPU and no Docker -- so the API, governance layer, reviewer UI and CI are all
     testable before Person A's models land.
  2. They ARE the baseline arm required by the acceptance-gate checklist
     ("at least one baseline comparison (rule-based extraction)"). When Person A's
     transformer extractor is wired in, this module stays as the control.

They are deliberately simple and deterministic: same input, same output, no
randomness, so tests and the comparison study are reproducible.

The STT baseline does not decode audio. It resolves text in priority order:

  1. the upload IS a transcript (`.txt`/`.vtt`/`.md`) -- read it directly;
  2. a sidecar `<upload>.txt` sits next to an audio upload -- read that;
  3. otherwise emit a clearly-labelled placeholder rather than inventing content.

Real transcription is Person A's `WhisperSTT`.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from backend.app.ml.base import (
    AgendaBlockResult,
    DiarizationResult,
    ExtractedAction,
    ExtractedDecision,
    ExtractionResult,
    Segment,
    SegmentationResult,
    TranscriptionResult,
)

# --------------------------------------------------------------------------- #
# Speech to text
# --------------------------------------------------------------------------- #

_DEVANAGARI = re.compile(r"[ऀ-ॿ]")
_LATIN = re.compile(r"[A-Za-z]")

PLACEHOLDER_NOTICE = (
    "[NO TRANSCRIPT AVAILABLE] The rule-based STT baseline cannot decode audio. "
    "Upload a .txt transcript instead, or enable the Whisper backend "
    "(MOM_STT_BACKEND=whisper) once Person A's model is wired in."
)


#: Extensions that are already text and need no transcription.
TRANSCRIPT_SUFFIXES = {".txt", ".vtt", ".md"}


class BaselineSTT:
    """Transcript reader standing in for a real STT engine."""

    name = "baseline-stt-v1"

    def transcribe(self, audio_path: Path, language_hint: str | None = None) -> TranscriptionResult:
        text, source = self._resolve_text(audio_path)
        segments = self._to_segments(text)
        languages = self._detect_languages(text, language_hint)
        return TranscriptionResult(
            text=text,
            segments=segments,
            detected_languages=languages,
            is_code_mixed=len(languages) > 1,
            # No audio is decoded, so duration is estimated from word count at a
            # typical speaking rate (~150 wpm). Labelled as an estimate, not a
            # measurement -- Person A's Whisper backend reports the real figure.
            duration_seconds=round(len(text.split()) / 2.5, 2),
            model_name=f"{self.name} ({source})",
        )

    @staticmethod
    def _resolve_text(path: Path) -> tuple[str, str]:
        """Return (text, provenance-label)."""
        if path.suffix.lower() in TRANSCRIPT_SUFFIXES and path.exists():
            body = path.read_text(encoding="utf-8", errors="replace").strip()
            if body:
                return body, "uploaded-transcript"

        sidecar = path.with_suffix(".txt")
        if sidecar.exists():
            body = sidecar.read_text(encoding="utf-8", errors="replace").strip()
            if body:
                return body, "sidecar-transcript"

        return PLACEHOLDER_NOTICE, "placeholder"

    @staticmethod
    def _to_segments(text: str) -> list[Segment]:
        """Split on sentence boundaries and assign synthetic timings so downstream
        code always sees a populated segment list."""
        parts = [p.strip() for p in re.split(r"(?<=[.!?।])\s+|\n+", text) if p.strip()]
        segments, cursor = [], 0.0
        for part in parts:
            duration = max(1.0, len(part.split()) / 2.5)
            segments.append(
                Segment(start=round(cursor, 2), end=round(cursor + duration, 2), text=part)
            )
            cursor += duration
        return segments

    @staticmethod
    def _detect_languages(text: str, hint: str | None) -> list[str]:
        found = []
        if _DEVANAGARI.search(text):
            found.append("hi")
        if _LATIN.search(text):
            found.append("en")
        if hint and hint not in found:
            found.append(hint)
        return found or ["unknown"]


class BaselineDiarizer:
    """Deterministic speaker assignment. Real diarization is Person A's pyannote module.

    Speakers are derived from a hash of the utterance so the labelling is stable
    across runs -- enough to exercise the UI and the schema, and honest about being
    a placeholder (`speaker_count` reflects what it actually produced).
    """

    name = "baseline-diarizer-v1"
    speakers = ("SPEAKER_00", "SPEAKER_01", "SPEAKER_02")

    def diarize(self, audio_path: Path, segments: list[Segment]) -> DiarizationResult:
        labelled = []
        for seg in segments:
            # Not a security primitive: a stable bucketing hash so the same
            # utterance always lands on the same placeholder speaker label.
            digest = hashlib.sha1(seg.text.encode("utf-8"), usedforsecurity=False).digest()[0]
            labelled.append(
                Segment(
                    start=seg.start,
                    end=seg.end,
                    text=seg.text,
                    speaker=self.speakers[digest % len(self.speakers)],
                )
            )
        distinct = len({s.speaker for s in labelled})
        return DiarizationResult(segments=labelled, speaker_count=distinct, model_name=self.name)


# --------------------------------------------------------------------------- #
# Agenda segmentation
# --------------------------------------------------------------------------- #

TOPIC_CUES = re.compile(
    r"\b(next (?:item|topic|up)|moving on|let'?s (?:discuss|talk about|move)|"
    r"agenda item|first item|second item|third item|next agenda|"
    r"turning to|on the subject of|now (?:for|about)|"
    r"अगला|अब हम)\b",
    re.I,
)


class BaselineAgendaSegmenter:
    """Cue-phrase segmentation. Person A's embedding/TextTiling segmenter replaces this."""

    name = "baseline-agenda-v1"
    target_sentences_per_block = 6

    def segment(self, text: str, segments: list[Segment] | None = None) -> SegmentationResult:
        sentences = self._sentences(text)
        if not sentences:
            return SegmentationResult(blocks=[], model_name=self.name)

        blocks: list[AgendaBlockResult] = []
        current: list[tuple[str, int, int]] = []

        def flush() -> None:
            if not current:
                return
            body = " ".join(s for s, _, _ in current)
            blocks.append(
                AgendaBlockResult(
                    position=len(blocks),
                    title=self._title(current[0][0], len(blocks)),
                    text=body,
                    start_char=current[0][1],
                    end_char=current[-1][2],
                    # Cue-phrase boundaries are more trustworthy than length-forced ones.
                    confidence=0.75 if TOPIC_CUES.search(current[0][0]) else 0.45,
                )
            )
            current.clear()

        for sentence, start, end in sentences:
            if current and (
                TOPIC_CUES.search(sentence) or len(current) >= self.target_sentences_per_block
            ):
                flush()
            current.append((sentence, start, end))
        flush()

        return SegmentationResult(blocks=blocks, model_name=self.name)

    @staticmethod
    def _sentences(text: str) -> list[tuple[str, int, int]]:
        out = []
        for m in re.finditer(r"[^.!?।\n]+[.!?।]?", text):
            s = m.group(0).strip()
            if s:
                out.append((s, m.start(), m.end()))
        return out

    @staticmethod
    def _title(first_sentence: str, index: int) -> str:
        cleaned = TOPIC_CUES.sub("", first_sentence).strip(" ,.:;-—")
        words = cleaned.split()
        if not words:
            return f"Agenda item {index + 1}"
        title = " ".join(words[:8])
        return title[:1].upper() + title[1:]


# --------------------------------------------------------------------------- #
# Decision / action extraction
# --------------------------------------------------------------------------- #

DECISION_CUES = re.compile(
    r"\b(we (?:have )?(?:decided|agreed|concluded|settled on)|"
    r"(?:it (?:was|is)|final) (?:decided|decision|agreed)|"
    r"the decision is|we(?:'| a)re going (?:to|with)|"
    r"approved|sign(?:ed)? off|we will go with|let'?s go with|"
    r"तय हुआ|फाइनल है)\b",
    re.I,
)

ACTION_CUES = re.compile(
    r"\b(will (?:send|share|prepare|update|review|draft|check|fix|deploy|call|follow up)|"
    r"needs? to|has to|should|action item|take(?:s)? (?:this|that) up|"
    r"assign(?:ed)? to|owner is|responsible for|please (?:send|share|prepare|update|check)|"
    r"I'?ll |we'?ll |follow[- ]up|by (?:eod|eow|tomorrow|next week|friday|monday))\b",
    re.I,
)

# "Priya will send", "Assigned to Rahul", "Owner: Sam"
OWNER_PATTERNS = [
    re.compile(r"\b([A-Z][a-z]{2,15})\s+(?:will|to|should|needs? to|has to|is going to)\b"),
    re.compile(r"\bassigned to\s+([A-Z][a-z]{2,15})", re.I),
    re.compile(r"\bowner\s*(?:is|:)\s*([A-Z][a-z]{2,15})", re.I),
    re.compile(r"\b([A-Z][a-z]{2,15})\s+(?:ko|ne)\b"),  # Hindi case markers in code-mixed speech
]

DEADLINE_PATTERNS = [
    re.compile(r"\bby\s+(\d{1,2}(?:st|nd|rd|th)?\s+\w+(?:\s+\d{4})?)", re.I),
    re.compile(r"\bby\s+(eod|eow|tomorrow|today|tonight|next week|this week|month end)\b", re.I),
    re.compile(r"\bby\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", re.I),
    re.compile(r"\b(\d{4}-\d{2}-\d{2})\b"),
    re.compile(r"\bbefore\s+(\w+day|the\s+\w+)\b", re.I),
]

# Words that make a sentence sound like a decision without being one.
HEDGES = re.compile(r"\b(maybe|might|perhaps|possibly|not sure|tentatively|if (?:we|it))\b", re.I)


class BaselineExtractor:
    """Keyword/pattern extractor. The control arm for Person A's model comparison."""

    name = "baseline-extractor-v1"

    def extract(self, blocks: list[AgendaBlockResult]) -> ExtractionResult:
        decisions: list[ExtractedDecision] = []
        actions: list[ExtractedAction] = []

        for block in blocks:
            for sentence in self._sentences(block.text):
                if DECISION_CUES.search(sentence):
                    decisions.append(
                        ExtractedDecision(
                            text=self._normalise(sentence),
                            confidence=self._score(sentence, base=0.62),
                            evidence_quote=sentence,
                            agenda_position=block.position,
                        )
                    )
                if ACTION_CUES.search(sentence):
                    actions.append(
                        ExtractedAction(
                            text=self._normalise(sentence),
                            owner_name=self._owner(sentence),
                            deadline=self._deadline(sentence),
                            confidence=self._score(sentence, base=0.58),
                            evidence_quote=sentence,
                            agenda_position=block.position,
                        )
                    )

        return ExtractionResult(decisions=decisions, actions=actions, model_name=self.name)

    @staticmethod
    def _sentences(text: str) -> list[str]:
        return [s.strip() for s in re.split(r"(?<=[.!?।])\s+", text) if s.strip()]

    @staticmethod
    def _normalise(sentence: str) -> str:
        return re.sub(r"\s+", " ", sentence).strip()

    @staticmethod
    def _score(sentence: str, base: float) -> float:
        """Crude but explainable confidence -- hedged or very short spans score lower.

        Explainability matters for grading: a reviewer can see why an item was
        flagged low-confidence and routed to a stricter gate.
        """
        score = base
        if HEDGES.search(sentence):
            score -= 0.25
        word_count = len(sentence.split())
        if word_count < 5:
            score -= 0.15
        elif word_count > 12:
            score += 0.08
        if any(p.search(sentence) for p in DEADLINE_PATTERNS):
            score += 0.10
        return round(max(0.05, min(0.95, score)), 2)

    @staticmethod
    def _owner(sentence: str) -> str | None:
        for pattern in OWNER_PATTERNS:
            m = pattern.search(sentence)
            if m:
                return m.group(1).strip()
        return None

    @staticmethod
    def _deadline(sentence: str) -> str | None:
        for pattern in DEADLINE_PATTERNS:
            m = pattern.search(sentence)
            if m:
                return m.group(1).strip()
        return None
