"""Embedding-based agenda segmentation.  ** PERSON A IMPLEMENTS THIS **

Status: stub. `MOM_SEGMENTER_BACKEND=embedding` raises NotImplementedError; the
registry falls back to the cue-phrase baseline.

This is where the blueprint's vector store (Chroma / Qdrant) earns its place. The
platform deliberately does not use one: the baseline segmenter is cue-phrase
based and needs no embeddings. If you add retrieval-augmented extraction or want
to persist embeddings across runs, the vector store belongs here, behind this
same Protocol -- the platform stays unaware of it.

What the platform needs back
---------------------------
A `SegmentationResult` of `AgendaBlockResult`s. Every field is used:

  position      0-based, contiguous, in transcript order. The reviewer editor
                and `persist_minutes` key agenda blocks by this.
  title         a short human-readable label, shown in the UI and exported in
                the CSV `agenda_item` column.
  text          the block's text. The extractor runs per block, and the evidence
                quotes it produces must appear verbatim in the stored transcript,
                so do not paraphrase or normalise the text here.
  start_char /  offsets into the transcript you were given. Used to trace a block
  end_char      back to its source span.
  confidence    0..1, how sure you are this is a real topic boundary. Show your
                working -- a segmenter that returns a flat 1.0 tells a reviewer
                nothing.

Contract notes
--------------
* `segment()` receives the **PII-scrubbed** transcript. Redaction placeholders
  like `[REDACTED:EMAIL]` will be in the text; they are semantically neutral and
  should not create spurious boundaries.
* Handle Devanagari and code-mixed text. `sentence-transformers` multilingual
  models (e.g. `paraphrase-multilingual-MiniLM-L12-v2`) handle this; an
  English-only model will segment Hindi passages badly and skew the
  language-wise subgroup breakdown in your evaluation.
* A transcript with no clear topic shifts must still return at least one block.
  The platform treats zero blocks as a failed run.

Suggested implementation
------------------------
    pip install sentence-transformers

    model = SentenceTransformer("paraphrase-multilingual-MiniLM-L12-v2")
    sentences = split_sentences(text)
    embeddings = model.encode(sentences, normalize_embeddings=True)

    # TextTiling-style: cosine similarity between adjacent windows, then cut at
    # local minima that fall below mean - k*stdev.
    sims = [float(embeddings[i] @ embeddings[i + 1]) for i in range(len(sentences) - 1)]

Derive `confidence` from how deep the similarity valley is -- a sharp drop is a
confident boundary, a shallow one is not. That gives the reviewer a real signal
and gives you something to write up in the error analysis.
"""

from __future__ import annotations

import os

from backend.app.ml.base import Segment, SegmentationResult

DEFAULT_MODEL = os.getenv("MOM_EMBEDDING_MODEL", "paraphrase-multilingual-MiniLM-L12-v2")


class EmbeddingAgendaSegmenter:
    """Topic segmentation by embedding similarity."""

    name = "embedding-texttiling-v1"

    def __init__(self, model_id: str = DEFAULT_MODEL) -> None:
        self.model_id = model_id
        self._model = None  # lazy

    def segment(self, text: str, segments: list[Segment] | None = None) -> SegmentationResult:
        raise NotImplementedError(
            "EmbeddingAgendaSegmenter is not implemented yet (Person A). "
            "The platform is running the cue-phrase baseline instead. "
            "See the module docstring for the required contract."
        )
