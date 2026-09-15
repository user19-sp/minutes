"""The ML seam: backend selection and graceful degradation.

The property under test: Person A can land a half-finished model, or one whose
dependencies are not installed, without taking the platform down. The registry
falls back to the baseline and says so loudly instead.
"""

from __future__ import annotations

import pytest

from backend.app.ml import registry as ml
from backend.app.ml.base import (
    AgendaSegmenter,
    DecisionActionExtractor,
    Diarizer,
    SpeechToText,
)


@pytest.fixture(autouse=True)
def _reset():
    ml.reset_cache()
    yield
    ml.reset_cache()


def test_baseline_backends_satisfy_the_protocols():
    """The Protocols are the contract; the baselines must actually meet it."""
    assert isinstance(ml.get_stt(), SpeechToText)
    assert isinstance(ml.get_diarizer(), Diarizer)
    assert isinstance(ml.get_segmenter(), AgendaSegmenter)
    assert isinstance(ml.get_extractor(), DecisionActionExtractor)


def test_person_a_stubs_satisfy_the_protocols():
    """The stubs are structurally correct, so wiring them in cannot break the
    platform -- only their bodies are missing."""
    from backend.app.ml.embedding_segmenter import EmbeddingAgendaSegmenter
    from backend.app.ml.pyannote_diarizer import PyannoteDiarizer
    from backend.app.ml.transformer_extractor import TransformerExtractor
    from backend.app.ml.whisper_stt import WhisperSTT

    assert isinstance(WhisperSTT(), SpeechToText)
    assert isinstance(PyannoteDiarizer(), Diarizer)
    assert isinstance(EmbeddingAgendaSegmenter(), AgendaSegmenter)
    assert isinstance(TransformerExtractor(), DecisionActionExtractor)


def test_every_backend_name_in_the_registry_is_importable():
    """Guards against a factory pointing at a module that does not exist."""
    for kind, factories in ml.BACKENDS.items():
        for choice in factories:
            instance = factories[choice]()
            assert getattr(instance, "name", None), f"{kind}/{choice} has no name"


def test_unknown_backend_is_rejected_loudly(monkeypatch):
    """A typo in configuration should fail fast, not silently use the baseline."""
    monkeypatch.setenv("MOM_STT_BACKEND", "definitely-not-a-backend")
    ml.reset_cache()
    with pytest.raises(ValueError, match="Unknown stt backend"):
        ml.get_stt()


def test_missing_dependencies_fall_back_to_baseline(monkeypatch):
    """A model whose libraries are not installed must not take the API down."""

    def _explode():
        raise ImportError("No module named 'faster_whisper'")

    monkeypatch.setitem(ml.BACKENDS["stt"], "whisper", _explode)
    monkeypatch.setenv("MOM_STT_BACKEND", "whisper")
    ml.reset_cache()

    stt = ml.get_stt()
    assert stt.name.startswith("baseline-stt"), "should have degraded to the baseline"


def test_active_backends_are_reported_for_traceability():
    """Every run stamps these, so a result can be traced to the models that
    produced it."""
    backends = ml.active_backends()
    assert set(backends) == {"stt", "diarizer", "segmenter", "extractor"}
    assert all(name and name != "?" for name in backends.values())


def test_stubs_raise_not_implemented_rather_than_returning_junk():
    """A stub must fail obviously. Returning empty results would look like a
    meeting where nothing was decided."""
    from pathlib import Path

    from backend.app.ml.transformer_extractor import TransformerExtractor
    from backend.app.ml.whisper_stt import WhisperSTT

    with pytest.raises(NotImplementedError, match="Person A"):
        WhisperSTT().transcribe(Path("anything.wav"))
    with pytest.raises(NotImplementedError, match="Person A"):
        TransformerExtractor().extract([])
