"""Decision and action extraction.  ** PERSON A IMPLEMENTS THIS **

Status: stub. `MOM_EXTRACTOR_BACKEND=transformer` raises NotImplementedError; the
registry falls back to the rule-based baseline.

This is the model the whole project is judged on, and the one the baseline
comparison measures against. `backend/app/ml/baseline.py` is the control arm --
leave it in place when this lands.

What the platform needs back
---------------------------
An `ExtractionResult` of `ExtractedDecision` and `ExtractedAction`. Two fields
are not optional, because platform tests enforce them:

  evidence_quote  A span that appears **verbatim** in the block text you were
                  given. `test_injected_text_does_not_become_an_unsupported_claim`
                  asserts every persisted item's quote is found in the stored
                  transcript. This is the anti-hallucination control (threat
                  T-05): if you cannot point at the words, do not emit the item.

  confidence      0..1 and meaningfully calibrated. The reviewer UI colours low
                  confidence amber so attention goes there, and hedged statements
                  are expected to score lower than firm ones -- there is a test
                  for that. A model that returns 0.99 for everything makes the
                  reviewer's triage useless.

Also populate `agenda_position` (copied from the block) so items attach to the
right agenda block, and `model_name` on the result for traceability.

Two implementation routes
-------------------------
1. **Fine-tuned classifier** (DistilBERT/BERT, per-sentence multi-label:
   decision / action / neither). Cheap, fast, fully offline, and its
   probabilities are naturally calibrated confidences. Owner and deadline then
   come from a separate extraction pass.

2. **Instruction-tuned LLM with structured output.** Better at owner/deadline
   and at code-mixed phrasing, but:

   * **The transcript is untrusted input.** Fence it before it reaches any
     prompt -- `backend/app/security/injection.py` provides
     `wrap_untrusted(text)` for exactly this, and `agent/tools.py` already builds
     the fenced form. Never interpolate raw transcript into an instruction.
   * Constrain the output to a schema and validate it. An LLM that returns prose
     where the platform expects records will fail the tool's argument validation
     and the call will be denied.
   * Do not let it invent a quote. Verify each returned `evidence_quote` is
     actually a substring of the block, and drop the item if it is not.

Whichever you pick, the governance layer already contains the blast radius: a
successful injection still cannot write or export without a human ruling.

Contract notes
--------------
* Raise `MLServiceError` on failure, not a transformers/torch exception.
* You receive PII-scrubbed text; `[REDACTED:EMAIL]` placeholders will appear.
  An action item whose owner was redacted should have `owner_name=None` rather
  than the placeholder string.
* Handle Devanagari and code-mixed input. `हमने तय किया` ("we decided") is a
  decision cue; an English-only model will miss it, and that gap will show up in
  your language-wise subgroup breakdown.
* A meeting where nothing was decided must return empty lists. There is a test
  (`test_empty_meeting_produces_no_invented_minutes`) that a social catch-up
  yields nothing -- over-extraction is as much a failure as under-extraction.

Evaluation hooks
----------------
`fixtures/meetings/DATASET.md` carries expected decision/action counts per
scenario as a starting reference, and `manifest.json` has them machine-readable.
For the dossier proper you need a properly annotated set with more than one
annotator and an agreement measure.
"""

from __future__ import annotations

import os

from backend.app.ml.base import AgendaBlockResult, ExtractionResult

DEFAULT_MODEL = os.getenv("MOM_EXTRACTOR_MODEL", "distilbert-base-multilingual-cased")


class TransformerExtractor:
    """Model-based decision and action-item extraction."""

    name = "transformer-extractor-v1"

    def __init__(self, model_id: str = DEFAULT_MODEL) -> None:
        self.model_id = model_id
        self._model = None  # lazy

    def extract(self, blocks: list[AgendaBlockResult]) -> ExtractionResult:
        raise NotImplementedError(
            "TransformerExtractor is not implemented yet (Person A). "
            "The platform is running the rule-based baseline instead. "
            "See the module docstring for the required contract."
        )
