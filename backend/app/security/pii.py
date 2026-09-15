"""PII scrubbing applied to transcript text *before* it is persisted.

Design note: this runs at the storage boundary, not at the display boundary, so a
database dump or a leaked backup contains no raw identifiers. Redaction is
irreversible by design -- we store no mapping back to the original value.

The patterns target the identifier types most likely to be spoken aloud in an
Indian corporate meeting (the deployment context in the problem brief): email,
phone, Aadhaar, PAN, GSTIN, card numbers, plus generic IP/URL credentials.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

__all__ = ["PATTERNS", "PIIFinding", "ScrubResult", "detect", "scrub_text"]


@dataclass(frozen=True)
class PIIFinding:
    kind: str
    start: int
    end: int
    value: str


@dataclass
class ScrubResult:
    text: str
    findings: list[PIIFinding] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.findings)

    @property
    def kinds(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for f in self.findings:
            out[f.kind] = out.get(f.kind, 0) + 1
        return out


def _luhn_ok(digits: str) -> bool:
    """Card-number checksum. Keeps 16-digit order numbers from being redacted."""
    total, parity = 0, len(digits) % 2
    for i, ch in enumerate(digits):
        d = int(ch)
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


# Order matters: earlier patterns win on overlap.
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("EMAIL", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]{2,}\b")),
    ("CARD", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
    ("AADHAAR", re.compile(r"\b[2-9]\d{3}[ -]?\d{4}[ -]?\d{4}\b")),
    ("PAN", re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")),
    ("GSTIN", re.compile(r"\b\d{2}[A-Z]{5}\d{4}[A-Z][\dA-Z]Z[\dA-Z]\b")),
    ("PHONE", re.compile(r"(?<!\d)(?:\+?\d{1,3}[ -]?)?(?:\d[ -]?){9,12}\d(?!\d)")),
    ("IP", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
    ("URL_CREDS", re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s/@]+:[^\s/@]+@\S+", re.I)),
]

_VALIDATORS = {
    # Only redact a long digit run if it actually checksums as a card number.
    "CARD": lambda v: _luhn_ok(re.sub(r"\D", "", v)) and 13 <= len(re.sub(r"\D", "", v)) <= 19,
    "AADHAAR": lambda v: len(re.sub(r"\D", "", v)) == 12,
    "IP": lambda v: all(0 <= int(p) <= 255 for p in v.split(".")),
}


def detect(text: str) -> list[PIIFinding]:
    """Return non-overlapping findings, ordered by position in the text."""
    claimed: list[tuple[int, int]] = []
    findings: list[PIIFinding] = []

    def overlaps(s: int, e: int) -> bool:
        return any(s < ce and cs < e for cs, ce in claimed)

    for kind, pattern in PATTERNS:
        validator = _VALIDATORS.get(kind)
        for m in pattern.finditer(text):
            s, e = m.span()
            if overlaps(s, e):
                continue
            value = m.group(0)
            if validator and not validator(value):
                continue
            claimed.append((s, e))
            findings.append(PIIFinding(kind=kind, start=s, end=e, value=value))

    findings.sort(key=lambda f: f.start)
    return findings


def scrub_text(text: str, enabled: bool = True) -> ScrubResult:
    """Replace every detected identifier with a `[REDACTED:KIND]` placeholder.

    Placeholders preserve readability for the reviewer while removing the value.
    """
    if not enabled or not text:
        return ScrubResult(text=text, findings=[])

    findings = detect(text)
    if not findings:
        return ScrubResult(text=text, findings=[])

    out, cursor = [], 0
    for f in findings:
        out.append(text[cursor : f.start])
        out.append(f"[REDACTED:{f.kind}]")
        cursor = f.end
    out.append(text[cursor:])
    return ScrubResult(text="".join(out), findings=findings)


def scrub_segments(segments: list[dict], enabled: bool = True) -> tuple[list[dict], int]:
    """Scrub the `text` field of each diarized segment. Returns (segments, redaction_count)."""
    if not enabled:
        return segments, 0
    total = 0
    scrubbed = []
    for seg in segments:
        result = scrub_text(str(seg.get("text", "")), enabled=True)
        total += result.count
        scrubbed.append({**seg, "text": result.text})
    return scrubbed, total
