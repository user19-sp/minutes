"""Speaker diarization with pyannote.audio.  ** PERSON A IMPLEMENTS THIS **

Status: stub. `MOM_DIARIZER_BACKEND=pyannote` raises NotImplementedError, which
`ml/registry.py` catches -- it falls back to the baseline and logs loudly.

Diarization is the blueprint's *optional* module, and the orchestrator treats it
that way: if this tool fails, the run records `stage.skipped` with
`outcome="degraded"` and carries on without speaker labels. Do not make the
pipeline depend on it succeeding.

What the platform needs back
---------------------------
A `DiarizationResult` whose `segments` are the STT segments you were handed, each
with `speaker` populated. You receive the transcript segments precisely so you
can align speakers to existing text rather than re-segmenting -- returning a
different segmentation would desynchronise the reviewer editor.

  speaker_count   report what you actually found, not a configured maximum.
  model_name      e.g. "pyannote/speaker-diarization-3.1".

Contract notes
--------------
* Raise `MLServiceError` on failure, not a pyannote or torch exception.
* pyannote's pipeline returns its own turn boundaries. You need to *map* those
  onto the STT segments -- the usual approach is to assign each STT segment the
  speaker whose turn overlaps it most.
* The model is gated on Hugging Face: it needs an accepted licence and an access
  token. Read it from the environment (`HF_TOKEN`); never commit it. The
  security scan in CI fails the build on a committed secret.
* Diarization is slow. It runs inside a tool call with a per-run budget, so keep
  an eye on `agent_tool_duration_seconds{tool="diarize_speakers"}`.

Suggested implementation
------------------------
    pip install pyannote.audio

    from pyannote.audio import Pipeline
    pipeline = Pipeline.from_pretrained(
        "pyannote/speaker-diarization-3.1",
        use_auth_token=os.environ["HF_TOKEN"],
    )
    annotation = pipeline(str(audio_path))

    for turn, _, speaker in annotation.itertracks(yield_label=True):
        ...  # turn.start, turn.end, speaker

Then, for each STT segment, pick the speaker with the greatest temporal overlap:

    overlap = min(seg.end, turn.end) - max(seg.start, turn.start)

A privacy note worth putting in the report: speaker labels are pseudonymous
(SPEAKER_00, SPEAKER_01). Do not map them to real names anywhere in storage --
that would reintroduce exactly the personal data the PII scrubber removes.
"""

from __future__ import annotations

import os
from pathlib import Path

from backend.app.ml.base import DiarizationResult, Segment

DEFAULT_MODEL = os.getenv("MOM_PYANNOTE_MODEL", "pyannote/speaker-diarization-3.1")


class PyannoteDiarizer:
    """Speaker segmentation and labelling."""

    name = "pyannote-3.1"

    def __init__(self, model_id: str = DEFAULT_MODEL) -> None:
        self.model_id = model_id
        self._pipeline = None  # lazy

    def diarize(self, audio_path: Path, segments: list[Segment]) -> DiarizationResult:
        raise NotImplementedError(
            "PyannoteDiarizer is not implemented yet (Person A). "
            "The platform is running the baseline diarizer instead. "
            "See the module docstring for the required contract."
        )
