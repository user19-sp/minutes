"""Whisper / faster-whisper speech-to-text.  ** PERSON A IMPLEMENTS THIS **

Status: stub. `MOM_STT_BACKEND=whisper` currently raises NotImplementedError,
which `ml/registry.py` catches -- it logs `ml_backend_unavailable` and falls back
to the baseline rather than taking the API down.

What the platform needs back
---------------------------
A `TranscriptionResult` (see `base.py`). Three fields matter beyond the text:

  model_name          stamped onto every extracted item for traceability. Include
                      the model size and compute type, e.g.
                      "faster-whisper-large-v3 (int8)". Unsourced output fails
                      the traceability tests.
  segments            timestamped `Segment`s. The reviewer editor renders these,
                      and diarization needs them to attach speaker labels.
  detected_languages  a list. Set `is_code_mixed=True` when more than one
                      language is present -- code-mixed handling is the core of
                      the problem statement, so it must be detected, not assumed.

Contract notes
--------------
* Raise `MLServiceError` on failure, not a whisper-specific exception. The
  orchestrator classifies failures without importing ML libraries.
* Long audio must be chunked; the platform passes whole files of up to
  MAX_UPLOAD_MB and does not chunk for you.
* This runs inside a tool call with a per-run time budget. Prefer faster-whisper
  (CTranslate2) over openai-whisper: roughly 4x faster at equal accuracy.
* Do not mutate anything on disk. Tools that call ML services are pure reads --
  that property is what makes the governance claim testable.

Suggested implementation
------------------------
    pip install faster-whisper

    from faster_whisper import WhisperModel
    self._model = WhisperModel(size, device="auto", compute_type="int8")
    segments, info = self._model.transcribe(
        str(audio_path), language=language_hint, vad_filter=True,
        word_timestamps=False,
    )

`info.language` and `info.language_probability` give you the detection. For
code-mixing, transcribe with `language=None` and inspect per-segment language,
or run a script-detection pass over the text as `baseline.py` does.

Load the model once (module-level or lazily on first call) -- reloading per
request will blow the tool time budget.
"""

from __future__ import annotations

import os
from pathlib import Path

from backend.app.ml.base import TranscriptionResult

DEFAULT_MODEL_SIZE = os.getenv("MOM_WHISPER_MODEL", "small")
DEFAULT_COMPUTE_TYPE = os.getenv("MOM_WHISPER_COMPUTE", "int8")


class WhisperSTT:
    """Multilingual, code-mixed-aware transcription."""

    name = f"whisper-{DEFAULT_MODEL_SIZE}"

    def __init__(
        self,
        model_size: str = DEFAULT_MODEL_SIZE,
        compute_type: str = DEFAULT_COMPUTE_TYPE,
    ) -> None:
        self.model_size = model_size
        self.compute_type = compute_type
        self._model = None  # lazy: do not pay model load at import time

    def transcribe(self, audio_path: Path, language_hint: str | None = None) -> TranscriptionResult:
        raise NotImplementedError(
            "WhisperSTT is not implemented yet (Person A). "
            "The platform is running the baseline STT instead. "
            "See the module docstring for the required contract."
        )
