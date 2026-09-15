"""Shared test fixtures.

Environment variables are set *before* any application module is imported, because
`backend.app.config` builds its Settings singleton (and `db` its engine) at import
time. pytest loads conftest first, so this is the one reliable place to do it.
"""

from __future__ import annotations

import os
import struct
import tempfile
import uuid
from pathlib import Path

import pytest

_TMP = Path(tempfile.mkdtemp(prefix="mom-tests-"))

# CI runs the suite twice: once on SQLite (fast, the local dev default) and once
# on Postgres (what we actually deploy on), by setting MOM_TEST_DATABASE_URL.
# Enum handling, JSON columns and transaction semantics differ between the two,
# so a green SQLite run alone does not prove the deployed system works.
_TEST_DB = os.environ.get("MOM_TEST_DATABASE_URL") or f"sqlite:///{(_TMP / 'test.db').as_posix()}"

os.environ.update(
    {
        "APP_ENV": "test",
        "LOG_LEVEL": "WARNING",
        "JWT_SECRET": "test-secret-not-used-anywhere-real-0123456789",
        "DATABASE_URL": _TEST_DB,
        "UPLOAD_DIR": str(_TMP / "uploads"),
        "EXPORT_DIR": str(_TMP / "exports"),
        "PII_SCRUBBING_ENABLED": "true",
        "AGENT_MAX_TOOL_CALLS": "25",
        "MOM_STT_BACKEND": "baseline",
        "MOM_EXTRACTOR_BACKEND": "baseline",
    }
)

from fastapi.testclient import TestClient  # noqa: E402

from backend.app.agent import tools as _tools  # noqa: E402,F401  (populates the allow-list)
from backend.app.db import Base, SessionLocal, engine  # noqa: E402
from backend.app.main import app  # noqa: E402
from backend.app.models import Role, User  # noqa: E402
from backend.app.security.auth import hash_password  # noqa: E402

SAMPLE_MEETING = """Good morning everyone, let us begin the sprint review for the payments module.
Priya will send the updated API contract to the vendor by Friday.
We have decided to postpone the Hindi localisation release until the next quarter.
Moving on to the next item, the infrastructure migration.
Rahul needs to check the staging database backups before the cutover.
It was agreed that we will go with the managed Postgres option.
Let us talk about the support backlog now.
Maybe we might look at the ticket triage process later, not sure yet.
Aarti will prepare the monthly compliance report by 2026-10-05.
The decision is final, we are going with the two week sprint cadence.
"""


def make_wav(path: Path, seconds: float = 0.2, sample_rate: int = 8000) -> Path:
    """Write a minimal but genuinely valid PCM WAV file.

    Hand-built rather than generated with `wave` so the header layout the
    ingestion sniffer checks is explicit in the test code.
    """
    n_samples = int(seconds * sample_rate)
    data = b"\x00\x00" * n_samples
    header = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE"
    header += b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, sample_rate, sample_rate * 2, 2, 16)
    header += b"data" + struct.pack("<I", len(data))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(header + data)
    return path


@pytest.fixture(scope="session", autouse=True)
def _schema():
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture(autouse=True)
def _clean_tables():
    """Truncate between tests so each one starts from a known state."""
    yield
    with SessionLocal() as db:
        for table in reversed(Base.metadata.sorted_tables):
            db.execute(table.delete())
        db.commit()


@pytest.fixture
def client() -> TestClient:
    with TestClient(app) as c:
        yield c


@pytest.fixture
def db():
    with SessionLocal() as session:
        yield session


def _make_user(email: str, password: str, role: Role) -> User:
    with SessionLocal() as session:
        user = User(
            email=email, hashed_password=hash_password(password), role=role, full_name=email
        )
        session.add(user)
        session.commit()
        session.refresh(user)
        return user


@pytest.fixture
def reviewer_factory():
    def _factory(role: Role = Role.REVIEWER, password: str = "correct-horse-battery"):
        email = f"{uuid.uuid4().hex[:10]}@example.com"
        user = _make_user(email, password, role)
        return user, email, password

    return _factory


@pytest.fixture
def auth(client: TestClient, reviewer_factory):
    """Returns (headers, user) for a logged-in reviewer."""

    def _login(role: Role = Role.REVIEWER):
        user, email, password = reviewer_factory(role)
        resp = client.post("/api/v1/auth/login", json={"email": email, "password": password})
        assert resp.status_code == 200, resp.text
        token = resp.json()["access_token"]
        return {"Authorization": f"Bearer {token}"}, user

    return _login


@pytest.fixture
def audio_file(tmp_path: Path):
    """A valid WAV plus the sidecar transcript the baseline STT reads."""

    def _make(transcript: str = SAMPLE_MEETING, name: str = "standup.wav") -> tuple[Path, str]:
        wav = make_wav(tmp_path / name)
        return wav, transcript

    return _make


@pytest.fixture
def uploaded_job(client: TestClient, audio_file):
    """Upload a recording and plant its sidecar transcript where STT will find it."""

    def _upload(headers: dict, transcript: str = SAMPLE_MEETING, title: str = "Sprint review"):
        wav, text = audio_file(transcript)
        with wav.open("rb") as fh:
            resp = client.post(
                "/api/v1/jobs",
                headers=headers,
                files={"file": (wav.name, fh, "audio/wav")},
                data={"title": title, "language_hint": "hi"},
            )
        assert resp.status_code == 201, resp.text
        job = resp.json()

        # The baseline STT reads <stored>.txt next to the stored recording.
        with SessionLocal() as session:
            from backend.app.models import Job

            stored = Path(session.get(Job, job["id"]).stored_path)
        stored.with_suffix(".txt").write_text(text, encoding="utf-8")
        return job

    return _upload
