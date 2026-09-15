"""Upload validation and storage.

Validation is defence-in-depth against malformed and hostile input (acceptance
gate: "malformed input" robustness experiment):

  * extension allow-list -- reject anything not an audio container we expect;
  * magic-byte sniffing  -- a .mp3 that is really a ZIP is rejected, because the
    declared extension and content type are both attacker-controlled;
  * size ceiling         -- enforced while streaming, not after buffering;
  * name sanitisation    -- the client filename never reaches the filesystem; we
    store under a generated UUID and keep the original as metadata only. This
    closes path traversal (`../../etc/passwd`) and Windows device names (`CON`).

Two kinds of input are accepted. AUDIO is the primary path. TRANSCRIPT (a .txt or
.vtt file of an already-written-up meeting) is accepted because the problem brief
covers "recordings *or notes*", and because it lets the platform, governance layer
and reviewer UI be exercised end-to-end before Person A's STT models land. A
transcript upload skips the STT stage; everything downstream is identical.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import BinaryIO

from backend.app.config import settings

AUDIO_EXTENSIONS = {".wav", ".mp3", ".m4a", ".mp4", ".flac", ".ogg", ".opus", ".webm", ".aac"}
TRANSCRIPT_EXTENSIONS = {".txt", ".vtt", ".md"}
ALLOWED_EXTENSIONS = AUDIO_EXTENSIONS | TRANSCRIPT_EXTENSIONS

#: Largest transcript we will accept, independent of the audio ceiling. A text
#: file that reaches this size is not a meeting transcript.
MAX_TRANSCRIPT_BYTES = 5 * 1024 * 1024

ALLOWED_CONTENT_TYPES = {
    "audio/wav",
    "audio/x-wav",
    "audio/wave",
    "audio/mpeg",
    "audio/mp3",
    "audio/mp4",
    "audio/x-m4a",
    "video/mp4",
    "audio/flac",
    "audio/x-flac",
    "audio/ogg",
    "audio/opus",
    "application/ogg",
    "audio/webm",
    "video/webm",
    "audio/aac",
    "application/octet-stream",  # many browsers send this for audio
}

TRANSCRIPT_CONTENT_TYPES = {
    "text/plain",
    "text/markdown",
    "text/vtt",
    "application/octet-stream",
}

#: (offset, magic bytes) pairs that identify a plausible audio/video container.
MAGIC_SIGNATURES: list[tuple[int, bytes]] = [
    (0, b"RIFF"),  # WAV
    (0, b"ID3"),  # MP3 with ID3 tag
    (0, b"\xff\xfb"),  # MP3 frame sync
    (0, b"\xff\xf3"),
    (0, b"\xff\xf2"),
    (0, b"\xff\xf1"),  # AAC ADTS
    (0, b"fLaC"),  # FLAC
    (0, b"OggS"),  # OGG / Opus
    (0, b"\x1a\x45\xdf\xa3"),  # Matroska / WebM
    (4, b"ftyp"),  # MP4 / M4A
]

SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]")
WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


class UploadRejected(Exception):
    """Validation failure. Callers map this to HTTP 400/413/415."""

    def __init__(self, reason: str, code: str = "invalid_upload") -> None:
        self.code = code
        super().__init__(reason)


@dataclass
class StoredUpload:
    path: Path
    size_bytes: int
    sha256: str
    safe_name: str
    #: "audio" or "transcript" -- decided from the extension, then confirmed
    #: against the actual bytes.
    kind: str = "audio"


def sanitise_filename(name: str) -> str:
    """Reduce a client-supplied name to something safe to echo back and log.

    The result is never used as the storage path -- see `store_upload`.

    `PureWindowsPath` rather than `Path`, deliberately: it treats both forward
    and back slashes as separators on every host, whereas `Path` on Linux treats
    a backslash as an ordinary character. The server runs on Linux and the
    clients run on Windows, so a Windows-style path in a filename would keep its
    directory part on the server and only be defanged by the character filter
    below. Parsing the client's path with the client's semantics is correct.
    """
    base = PureWindowsPath(name or "upload").name  # strips any directory component
    base = SAFE_NAME.sub("_", base).lstrip(".") or "upload"
    # Defence in depth: nothing that reads as a traversal should survive, even
    # though the generated storage path already makes traversal impossible.
    base = base.replace("..", "_")
    stem, _, _ext = base.rpartition(".")
    if (stem or base).upper() in WINDOWS_RESERVED:
        base = f"file_{base}"
    return base[:255] or "upload"


def validate_extension(filename: str) -> tuple[str, str]:
    """Return (extension, kind). Kind is "audio" or "transcript"."""
    ext = Path(sanitise_filename(filename)).suffix.lower()
    if ext in AUDIO_EXTENSIONS:
        return ext, "audio"
    if ext in TRANSCRIPT_EXTENSIONS:
        return ext, "transcript"
    raise UploadRejected(
        f"Unsupported file type {ext or '(none)'}. "
        f"Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}",
        code="unsupported_extension",
    )


def validate_content_type(content_type: str | None, kind: str = "audio") -> str:
    ct = (content_type or "application/octet-stream").split(";")[0].strip().lower()
    permitted = TRANSCRIPT_CONTENT_TYPES if kind == "transcript" else ALLOWED_CONTENT_TYPES
    if ct not in permitted:
        raise UploadRejected(f"Unsupported content type {ct!r}.", code="unsupported_content_type")
    return ct


def looks_like_audio(header: bytes) -> bool:
    """Magic-byte sniff. Declared type and extension are both untrusted."""
    return any(header[offset : offset + len(sig)] == sig for offset, sig in MAGIC_SIGNATURES)


def looks_like_text(chunk: bytes) -> bool:
    """A transcript has no magic number, so validate it structurally instead.

    Reject anything with NUL bytes or that is not valid UTF-8: that is how a
    binary payload renamed to `.txt` gets caught. Decoding is done on a chunk
    boundary-safe basis so a multi-byte character split across reads is not
    mistaken for corruption.
    """
    if 0 in chunk:  # a NUL byte never appears in a text transcript
        return False
    try:
        chunk.decode("utf-8")
        return True
    except UnicodeDecodeError as exc:
        # A truncated final character is fine; anything else is not text.
        return exc.start >= len(chunk) - 4


def store_upload(
    fileobj: BinaryIO,
    original_filename: str,
    content_type: str | None,
    *,
    max_bytes: int | None = None,
    sniff: bool = True,
) -> StoredUpload:
    """Stream an upload to disk under a generated name, hashing as we go.

    Streaming (rather than `read()`) keeps a 200 MB recording off the heap, and
    lets the size ceiling abort early instead of after the fact.
    """
    ext, kind = validate_extension(original_filename)
    validate_content_type(content_type, kind)
    safe_name = sanitise_filename(original_filename)

    if kind == "transcript":
        limit = min(max_bytes or MAX_TRANSCRIPT_BYTES, MAX_TRANSCRIPT_BYTES)
    else:
        limit = max_bytes or settings.allowed_upload_bytes

    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    target = settings.upload_dir / f"{uuid.uuid4()}{ext}"

    digest = hashlib.sha256()
    written = 0
    first_chunk = True

    try:
        with target.open("wb") as out:
            while chunk := fileobj.read(1024 * 1024):
                if first_chunk:
                    first_chunk = False
                    if sniff and kind == "audio" and not looks_like_audio(chunk[:16]):
                        raise UploadRejected(
                            "File content does not match any supported audio format.",
                            code="content_mismatch",
                        )
                    if sniff and kind == "transcript" and not looks_like_text(chunk):
                        raise UploadRejected(
                            "File is not valid UTF-8 text.",
                            code="content_mismatch",
                        )
                written += len(chunk)
                if written > limit:
                    raise UploadRejected(
                        f"Upload exceeds the {limit // (1024 * 1024)} MB limit.",
                        code="too_large",
                    )
                digest.update(chunk)
                out.write(chunk)

        if written == 0:
            raise UploadRejected("Uploaded file is empty.", code="empty_file")
        if kind == "transcript" and not target.read_text(encoding="utf-8", errors="ignore").strip():
            raise UploadRejected("Transcript contains no text.", code="empty_file")
    except Exception:
        target.unlink(missing_ok=True)  # never leave a partial file behind
        raise

    return StoredUpload(
        path=target,
        size_bytes=written,
        sha256=digest.hexdigest(),
        safe_name=safe_name,
        kind=kind,
    )


def is_transcript(path: str | Path) -> bool:
    return Path(path).suffix.lower() in TRANSCRIPT_EXTENSIONS


def delete_upload(path: str | Path) -> bool:
    """Remove a stored recording, refusing any path outside the upload directory."""
    p = Path(path).resolve()
    root = settings.upload_dir.resolve()
    if not p.is_relative_to(root):
        raise UploadRejected("Refusing to delete a path outside the upload directory.", "bad_path")
    if p.exists():
        p.unlink()
        return True
    return False


def purge_uploads() -> int:
    """Test/ops helper: clear stored recordings."""
    root = settings.upload_dir
    if not root.exists():
        return 0
    count = sum(1 for p in root.iterdir() if p.is_file())
    shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)
    return count
