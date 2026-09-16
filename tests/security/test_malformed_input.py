"""Malformed-input and robustness tests (acceptance gate: failure-mode experiment).

Covers hostile uploads, degenerate transcripts and authentication edge cases. The
standard the system is held to: refuse cleanly with a useful status code, never
crash, never write a partial artifact, and never leak a stack trace.
"""

from __future__ import annotations

import io

import pytest

from backend.app.config import settings
from backend.app.models import Role
from backend.app.services import ingestion

# --------------------------------------------------------------------------- #
# Upload validation
# --------------------------------------------------------------------------- #


def _upload(client, headers, name: str, content: bytes, content_type: str = "audio/wav"):
    return client.post(
        "/api/v1/jobs",
        headers=headers,
        files={"file": (name, io.BytesIO(content), content_type)},
        data={"title": "Robustness probe"},
    )


VALID_WAV_HEADER = b"RIFF$\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00@\x1f\x00\x00\x80>\x00\x00\x02\x00\x10\x00data\x00\x00\x00\x00"


def test_executable_disguised_as_audio_is_rejected(client, auth):
    """The extension says .wav; the magic bytes say Windows PE."""
    headers, _ = auth(Role.REVIEWER)
    resp = _upload(client, headers, "meeting.wav", b"MZ\x90\x00" + b"\x00" * 200)
    assert resp.status_code == 400
    assert "does not match any supported audio format" in resp.json()["detail"]


def test_zip_disguised_as_audio_is_rejected(client, auth):
    headers, _ = auth(Role.REVIEWER)
    resp = _upload(client, headers, "meeting.mp3", b"PK\x03\x04" + b"\x00" * 200, "audio/mpeg")
    assert resp.status_code == 400


def test_unsupported_extension_is_rejected(client, auth):
    headers, _ = auth(Role.REVIEWER)
    resp = _upload(client, headers, "notes.exe", VALID_WAV_HEADER, "application/octet-stream")
    assert resp.status_code == 415
    assert "Unsupported file type" in resp.json()["detail"]


def test_empty_file_is_rejected(client, auth):
    headers, _ = auth(Role.REVIEWER)
    resp = _upload(client, headers, "silence.wav", b"")
    assert resp.status_code == 400


def test_rejected_upload_leaves_no_file_on_disk(client, auth):
    """A failed upload must not leave a partial recording behind."""
    headers, _ = auth(Role.REVIEWER)
    before = set(settings.upload_dir.iterdir()) if settings.upload_dir.exists() else set()

    _upload(client, headers, "bad.wav", b"MZ\x90\x00" + b"\x00" * 500)

    after = set(settings.upload_dir.iterdir()) if settings.upload_dir.exists() else set()
    assert after == before, "a rejected upload left a file behind"


@pytest.mark.parametrize(
    "hostile",
    [
        "../../../../etc/passwd.wav",
        "..\\..\\windows\\system32\\evil.wav",
        "CON.wav",
        "meeting\x00.wav",
        "a" * 400 + ".wav",
    ],
)
def test_hostile_filenames_are_neutralised(hostile: str):
    """Path traversal, Windows device names, null bytes and overlong names."""
    safe = ingestion.sanitise_filename(hostile)
    assert "/" not in safe and "\\" not in safe
    assert ".." not in safe
    assert "\x00" not in safe
    assert len(safe) <= 255
    assert safe.upper().split(".")[0] not in ingestion.WINDOWS_RESERVED


def test_stored_path_is_generated_not_client_supplied(client, auth):
    """The client filename is metadata only; storage uses a generated UUID."""
    headers, _ = auth(Role.REVIEWER)
    resp = _upload(client, headers, "../../escape.wav", VALID_WAV_HEADER)
    assert resp.status_code == 201

    from backend.app.db import SessionLocal
    from backend.app.models import Job

    with SessionLocal() as db:
        job = db.get(Job, resp.json()["id"])
        stored = job.stored_path

    assert ".." not in stored
    assert stored.endswith(".wav")
    # The traversal prefix is stripped entirely: only the bare basename is kept as
    # display metadata, and it never participates in the storage path.
    assert resp.json()["original_filename"] == "escape.wav"
    assert "escape.wav" not in stored


def test_delete_refuses_paths_outside_the_upload_directory():
    with pytest.raises(ingestion.UploadRejected, match="outside the upload directory"):
        ingestion.delete_upload("C:/Windows/System32/drivers/etc/hosts")


# --------------------------------------------------------------------------- #
# Degenerate pipeline input
# --------------------------------------------------------------------------- #


def test_blank_transcript_yields_a_labelled_placeholder_not_invented_minutes(
    client, auth, uploaded_job
):
    """A blank sidecar is "no transcript available", not a crash and not content.

    The run proceeds so the reviewer sees an explicit placeholder explaining why
    there is no text, rather than an opaque run failure. The critical property is
    that no decisions or actions are manufactured out of the placeholder wording.
    """
    headers, _ = auth(Role.REVIEWER)
    job = uploaded_job(headers, transcript="   ")

    resp = client.post(f"/api/v1/jobs/{job['id']}/runs", headers=headers, json={"mode": "agent"})
    assert resp.status_code == 201
    assert resp.json()["status"] == "awaiting_approval"

    transcript = client.get(f"/api/v1/jobs/{job['id']}/transcript", headers=headers).json()
    assert "NO TRANSCRIPT AVAILABLE" in transcript["text"]
    # Provenance says placeholder, so nothing downstream can mistake it for speech.
    assert "placeholder" in transcript["model_name"]

    gate = client.get(f"/api/v1/jobs/{job['id']}/minutes", headers=headers).json()[
        "pending_approvals"
    ][0]
    assert gate["payload"]["decisions"] == []
    assert gate["payload"]["actions"] == []


def test_transcript_with_no_decisions_or_actions_still_completes(client, auth, uploaded_job):
    """A meeting where nothing was decided is a valid outcome, not a failure."""
    headers, _ = auth(Role.REVIEWER)
    job = uploaded_job(
        headers,
        transcript="Hello everyone. The weather is nice today. Thanks for joining. Goodbye.",
    )
    run = client.post(
        f"/api/v1/jobs/{job['id']}/runs", headers=headers, json={"mode": "agent"}
    ).json()

    assert run["status"] in ("awaiting_approval", "completed")
    minutes = client.get(f"/api/v1/jobs/{job['id']}/minutes", headers=headers).json()
    assert minutes["agenda_blocks"], "segmentation should still produce a block"


def test_very_long_transcript_is_handled(client, auth, uploaded_job):
    """A long meeting must not blow up segmentation or the extractor."""
    headers, _ = auth(Role.REVIEWER)
    long_text = (
        "We have decided to proceed with the plan. Priya will send the report by Friday. "
    ) * 400
    job = uploaded_job(headers, transcript=long_text)

    run = client.post(
        f"/api/v1/jobs/{job['id']}/runs", headers=headers, json={"mode": "agent"}
    ).json()
    assert run["status"] == "awaiting_approval"
    assert run["tool_call_count"] <= settings.agent_max_tool_calls


def test_code_mixed_transcript_is_detected(client, auth, uploaded_job):
    """Devanagari plus Latin script must be flagged as code-mixed."""
    headers, _ = auth(Role.REVIEWER)
    mixed = (
        "Team, aaj ka agenda hai payments module. "
        "\u0939\u092e\u0928\u0947 \u0924\u092f \u0915\u093f\u092f\u093e vendor ko switch karenge. "
        "Priya will send the contract by Friday."
    )
    job = uploaded_job(headers, transcript=mixed)
    client.post(f"/api/v1/jobs/{job['id']}/runs", headers=headers, json={"mode": "agent"})

    transcript = client.get(f"/api/v1/jobs/{job['id']}/transcript", headers=headers).json()
    assert transcript["is_code_mixed"] is True
    assert set(transcript["detected_languages"]) >= {"hi", "en"}


def test_running_a_job_twice_concurrently_is_refused(client, auth, uploaded_job):
    headers, _ = auth(Role.REVIEWER)
    job = uploaded_job(headers)
    client.post(f"/api/v1/jobs/{job['id']}/runs", headers=headers, json={"mode": "agent"})

    second = client.post(f"/api/v1/jobs/{job['id']}/runs", headers=headers, json={"mode": "agent"})
    assert second.status_code == 409
    assert "awaiting_approval" in second.json()["detail"]


# --------------------------------------------------------------------------- #
# Authentication edge cases
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "header",
    [
        {},
        {"Authorization": "Bearer "},
        {"Authorization": "Bearer not-a-jwt"},
        {"Authorization": "Basic dXNlcjpwYXNz"},
        {"Authorization": "Bearer eyJhbGciOiJub25lIn0.eyJzdWIiOiJhZG1pbiJ9."},
    ],
)
def test_malformed_credentials_are_rejected(client, header: dict):
    resp = client.get("/api/v1/jobs", headers=header)
    assert resp.status_code == 401


def test_alg_none_token_is_rejected():
    """Classic JWT bypass: an unsigned token claiming admin must not verify."""
    import jwt

    from backend.app.security.auth import AuthError, decode_access_token

    forged = jwt.encode({"sub": "admin", "role": "admin"}, key="", algorithm="none")
    with pytest.raises(AuthError):
        decode_access_token(forged)


def test_token_signed_with_the_wrong_secret_is_rejected():
    import jwt

    from backend.app.security.auth import AuthError, decode_access_token

    forged = jwt.encode(
        {"sub": "x", "role": "admin", "exp": 9_999_999_999, "iss": "mom-platform"},
        "attacker-secret-long-enough-to-avoid-a-length-warning",
        algorithm="HS256",
    )
    with pytest.raises(AuthError):
        decode_access_token(forged)


def test_expired_token_is_rejected():
    import jwt

    from backend.app.config import settings as app_settings
    from backend.app.security.auth import AuthError, decode_access_token

    expired = jwt.encode(
        {"sub": "x", "role": "admin", "exp": 1_000_000, "iss": "mom-platform"},
        app_settings.jwt_secret,
        algorithm="HS256",
    )
    with pytest.raises(AuthError):
        decode_access_token(expired)


def test_role_is_read_from_the_database_not_the_token(client, auth, db):
    """A stolen token cannot outlive a demotion."""
    headers, user = auth(Role.ADMIN)
    assert client.get("/api/v1/audit", headers=headers).status_code == 200

    from backend.app.models import User

    db.get(User, user.id).role = Role.REVIEWER
    db.commit()

    # Same token, demoted account: the admin route is now refused.
    assert client.get("/api/v1/audit", headers=headers).status_code == 403


def test_self_registration_cannot_grant_admin(client, approve_email):
    approve_email("escalate@example.com")
    resp = client.post(
        "/api/v1/auth/register",
        json={
            "email": "escalate@example.com",
            "password": "a-long-enough-password",
            "role": "admin",
        },
    )
    assert resp.status_code == 201
    assert resp.json()["role"] == "reviewer", "self-registration must not grant admin"


def test_short_password_is_refused(client):
    resp = client.post(
        "/api/v1/auth/register", json={"email": "weak@example.com", "password": "short"}
    )
    assert resp.status_code == 422


def test_errors_do_not_leak_stack_traces(client, auth):
    headers, _ = auth(Role.REVIEWER)
    resp = client.get("/api/v1/jobs/" + "0" * 36, headers=headers)
    body = resp.text
    assert "Traceback" not in body
    assert "backend/app" not in body
    assert "sqlalchemy" not in body.lower()


def test_validation_errors_do_not_echo_the_submitted_body(client, auth, uploaded_job):
    """A reflected body is an XSS/exfiltration gadget; return field names only."""
    headers, _ = auth(Role.REVIEWER)
    job = uploaded_job(headers)  # a real job, so we reach body validation not 404
    marker = "<script>alert(1)</script>"
    resp = client.post(f"/api/v1/jobs/{job['id']}/runs", headers=headers, json={"mode": marker})
    assert resp.status_code == 422
    assert marker not in resp.text
    assert "errors" in resp.json()


def test_security_headers_are_present(client):
    resp = client.get("/health")
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert resp.headers["Referrer-Policy"] == "no-referrer"
    assert resp.headers["X-Trace-Id"]
