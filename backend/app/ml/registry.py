"""Binds ML backends by configuration.

Person A adds a module (e.g. `backend/app/ml/whisper_stt.py`) exporting a class
that satisfies the matching Protocol in `base.py`, registers it here, and the
platform picks it up via environment variables -- no changes anywhere else:

    MOM_STT_BACKEND=whisper
    MOM_DIARIZER_BACKEND=pyannote
    MOM_SEGMENTER_BACKEND=embedding
    MOM_EXTRACTOR_BACKEND=transformer

Backends are imported lazily so that selecting `baseline` never imports torch.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from backend.app.ml.base import (
    AgendaSegmenter,
    DecisionActionExtractor,
    Diarizer,
    SpeechToText,
)
from backend.app.observability.logging import get_logger

log = get_logger("ml.registry")


def _baseline_stt() -> Any:
    from backend.app.ml.baseline import BaselineSTT

    return BaselineSTT()


def _baseline_diarizer() -> Any:
    from backend.app.ml.baseline import BaselineDiarizer

    return BaselineDiarizer()


def _baseline_segmenter() -> Any:
    from backend.app.ml.baseline import BaselineAgendaSegmenter

    return BaselineAgendaSegmenter()


def _baseline_extractor() -> Any:
    from backend.app.ml.baseline import BaselineExtractor

    return BaselineExtractor()


def _whisper_stt() -> Any:
    # Person A: implement WhisperSTT in backend/app/ml/whisper_stt.py
    from backend.app.ml.whisper_stt import WhisperSTT

    return WhisperSTT()


def _pyannote_diarizer() -> Any:
    from backend.app.ml.pyannote_diarizer import PyannoteDiarizer

    return PyannoteDiarizer()


def _embedding_segmenter() -> Any:
    from backend.app.ml.embedding_segmenter import EmbeddingAgendaSegmenter

    return EmbeddingAgendaSegmenter()


def _transformer_extractor() -> Any:
    from backend.app.ml.transformer_extractor import TransformerExtractor

    return TransformerExtractor()


BACKENDS: dict[str, dict[str, Callable[[], Any]]] = {
    "stt": {"baseline": _baseline_stt, "whisper": _whisper_stt},
    "diarizer": {"baseline": _baseline_diarizer, "pyannote": _pyannote_diarizer},
    "segmenter": {"baseline": _baseline_segmenter, "embedding": _embedding_segmenter},
    "extractor": {"baseline": _baseline_extractor, "transformer": _transformer_extractor},
}

ENV_KEYS = {
    "stt": "MOM_STT_BACKEND",
    "diarizer": "MOM_DIARIZER_BACKEND",
    "segmenter": "MOM_SEGMENTER_BACKEND",
    "extractor": "MOM_EXTRACTOR_BACKEND",
}

_cache: dict[str, Any] = {}


def _load(kind: str) -> Any:
    choice = os.getenv(ENV_KEYS[kind], "baseline").lower()
    factories = BACKENDS[kind]
    if choice not in factories:
        raise ValueError(f"Unknown {kind} backend {choice!r}. Available: {sorted(factories)}")
    try:
        instance = factories[choice]()
    except ImportError as exc:
        # A model backend whose dependencies are not installed must not take the
        # platform down -- fall back to the baseline and say so loudly.
        log.error(
            "ml_backend_unavailable",
            kind=kind,
            requested=choice,
            error=str(exc),
            fallback="baseline",
        )
        instance = factories["baseline"]()
    log.info("ml_backend_bound", kind=kind, backend=getattr(instance, "name", "?"))
    return instance


def get_stt() -> SpeechToText:
    return _cache.setdefault("stt", _load("stt"))


def get_diarizer() -> Diarizer:
    return _cache.setdefault("diarizer", _load("diarizer"))


def get_segmenter() -> AgendaSegmenter:
    return _cache.setdefault("segmenter", _load("segmenter"))


def get_extractor() -> DecisionActionExtractor:
    return _cache.setdefault("extractor", _load("extractor"))


def active_backends() -> dict[str, str]:
    """Reported by /health and stamped on every run, so an evaluation result can
    always be traced to the exact model set that produced it."""
    return {
        "stt": getattr(get_stt(), "name", "?"),
        "diarizer": getattr(get_diarizer(), "name", "?"),
        "segmenter": getattr(get_segmenter(), "name", "?"),
        "extractor": getattr(get_extractor(), "name", "?"),
    }


def reset_cache() -> None:
    """Test hook -- forces re-binding after env vars change."""
    _cache.clear()
