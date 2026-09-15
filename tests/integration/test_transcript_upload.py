"""Transcript-upload path, and a sweep of the whole synthetic fixture corpus.

The corpus sweep is the robustness experiment the acceptance gate asks for: every
scenario -- code-mixed, PII-laden, adversarial, empty, hedged, disfluent, long --
goes through the full governed pipeline and must behave, not merely not crash.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from backend.app.models import Role

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "meetings"


def _upload_text(client, headers, name: str, body: str, title: str = "Uploaded notes"):
    return client.post(
        "/api/v1/jobs",
        headers=headers,
        files={"file": (name, io.BytesIO(body.encode("utf-8")), "text/plain")},
        data={"title": title},
    )


# --------------------------------------------------------------------------- #
# Transcript upload
# --------------------------------------------------------------------------- #


def test_transcript_upload_runs_the_full_pipeline(client, auth):
    """A .txt upload needs no audio and no sidecar: it is the transcript."""
    headers, _ = auth(Role.REVIEWER)
    body = (
        "Welcome to the planning meeting.\n"
        "We have decided to move the launch to November.\n"
        "Priya will send the revised timeline by Wednesday.\n"
    )

    created = _upload_text(client, headers, "notes.txt", body)
    assert created.status_code == 201, created.text
    job = created.json()

    run = client.post(
        f"/api/v1/jobs/{job['id']}/runs", headers=headers, json={"mode": "agent"}
    ).json()
    assert run["status"] == "awaiting_approval"

    transcript = client.get(f"/api/v1/jobs/{job['id']}/transcript", headers=headers).json()
    assert "move the launch to November" in transcript["text"]
    # Provenance is recorded: this text came from the upload, not from a model.
    assert "uploaded-transcript" in transcript["model_name"]

    gate = client.get(f"/api/v1/jobs/{job['id']}/minutes", headers=headers).json()[
        "pending_approvals"
    ][0]
    client.post(
        f"/api/v1/approvals/{gate['id']}/decide", headers=headers, json={"decision": "approved"}
    )

    minutes = client.get(f"/api/v1/jobs/{job['id']}/minutes", headers=headers).json()
    assert any("November" in d["text"] for d in minutes["decisions"])
    assert any("Priya" in (a["owner_name"] or "") for a in minutes["action_items"])


def test_binary_disguised_as_transcript_is_rejected(client, auth):
    """A .txt that is really a PNG must not get through the text sniffer."""
    headers, _ = auth(Role.REVIEWER)
    png = bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A]) + bytes(200)

    resp = client.post(
        "/api/v1/jobs",
        headers=headers,
        files={"file": ("notes.txt", io.BytesIO(png), "text/plain")},
        data={"title": "Sneaky"},
    )
    assert resp.status_code == 400
    assert "not valid UTF-8 text" in resp.json()["detail"]


def test_whitespace_only_transcript_is_rejected(client, auth):
    headers, _ = auth(Role.REVIEWER)
    resp = _upload_text(client, headers, "blank.txt", "   \n\n\t  \n")
    assert resp.status_code == 400
    assert "no text" in resp.json()["detail"].lower()


def test_devanagari_transcript_survives_upload(client, auth):
    """Multi-byte UTF-8 must not be mangled by the sniffer or by storage."""
    headers, _ = auth(Role.REVIEWER)
    body = (
        "हमने तय किया है ki "
        "sprint cadence two weeks rahegi.\n"
        "Priya will send the contract by Friday.\n"
    )
    job = _upload_text(client, headers, "hindi.txt", body).json()
    client.post(f"/api/v1/jobs/{job['id']}/runs", headers=headers, json={"mode": "agent"})

    transcript = client.get(f"/api/v1/jobs/{job['id']}/transcript", headers=headers).json()
    assert "हमने तय किया" in transcript["text"]
    assert transcript["is_code_mixed"] is True


def test_audio_upload_without_a_transcript_is_labelled_not_invented(client, auth, audio_file):
    """With no text available the baseline must say so, not fabricate minutes."""
    headers, _ = auth(Role.REVIEWER)
    wav, _ = audio_file()
    with wav.open("rb") as fh:
        job = client.post(
            "/api/v1/jobs",
            headers=headers,
            files={"file": ("lonely.wav", fh, "audio/wav")},
            data={"title": "No transcript"},
        ).json()

    client.post(f"/api/v1/jobs/{job['id']}/runs", headers=headers, json={"mode": "agent"})
    transcript = client.get(f"/api/v1/jobs/{job['id']}/transcript", headers=headers).json()

    assert "NO TRANSCRIPT AVAILABLE" in transcript["text"]
    assert "placeholder" in transcript["model_name"]


# --------------------------------------------------------------------------- #
# Fixture corpus sweep
# --------------------------------------------------------------------------- #


def _corpus() -> list[dict]:
    manifest = FIXTURES / "manifest.json"
    if not manifest.exists():
        return []
    return json.loads(manifest.read_text(encoding="utf-8"))["scenarios"]


CORPUS = _corpus()


@pytest.mark.skipif(not CORPUS, reason="run scripts/make_fixtures.py to generate the corpus")
@pytest.mark.parametrize("scenario", CORPUS, ids=[s["slug"] for s in CORPUS])
def test_every_fixture_scenario_runs_cleanly(client, auth, scenario):
    """Each scenario must reach a gate (or complete) without failing the run."""
    headers, _ = auth(Role.REVIEWER)
    body = (FIXTURES / scenario["transcript"]).read_text(encoding="utf-8")

    job = _upload_text(
        client, headers, scenario["transcript"], body, title=scenario["title"]
    ).json()
    run = client.post(
        f"/api/v1/jobs/{job['id']}/runs", headers=headers, json={"mode": "agent"}
    ).json()

    assert run["status"] == "awaiting_approval", (
        f"{scenario['slug']} did not reach the gate: {run['status']} / {run.get('error_message')}"
    )
    assert run["denied_tool_call_count"] == 0
    assert run["tool_call_count"] <= 25

    transcript = client.get(f"/api/v1/jobs/{job['id']}/transcript", headers=headers).json()
    assert transcript["is_code_mixed"] == scenario["expected"]["code_mixed"], (
        f"{scenario['slug']} code-mixing detection disagrees with the dataset card"
    )

    gate = client.get(f"/api/v1/jobs/{job['id']}/minutes", headers=headers).json()[
        "pending_approvals"
    ][0]
    client.post(
        f"/api/v1/approvals/{gate['id']}/decide", headers=headers, json={"decision": "approved"}
    )
    minutes = client.get(f"/api/v1/jobs/{job['id']}/minutes", headers=headers).json()

    # Every persisted item must be evidence-backed, whatever the scenario.
    for item in minutes["decisions"] + minutes["action_items"]:
        assert item["evidence_quote"] in transcript["text"]


@pytest.mark.skipif(not CORPUS, reason="run scripts/make_fixtures.py to generate the corpus")
def test_empty_meeting_produces_no_invented_minutes(client, auth):
    """Over-extraction check: a social catch-up must yield nothing."""
    headers, _ = auth(Role.REVIEWER)
    body = (FIXTURES / "05_no_decisions_social.txt").read_text(encoding="utf-8")

    job = _upload_text(client, headers, "social.txt", body).json()
    client.post(f"/api/v1/jobs/{job['id']}/runs", headers=headers, json={"mode": "agent"})

    gate = client.get(f"/api/v1/jobs/{job['id']}/minutes", headers=headers).json()[
        "pending_approvals"
    ][0]
    assert gate["payload"]["decisions"] == []
    assert gate["payload"]["actions"] == []


@pytest.mark.skipif(not CORPUS, reason="run scripts/make_fixtures.py to generate the corpus")
def test_pii_fixture_is_fully_redacted(client, auth):
    headers, _ = auth(Role.REVIEWER)
    body = (FIXTURES / "03_pii_heavy_onboarding.txt").read_text(encoding="utf-8")

    job = _upload_text(client, headers, "onboarding.txt", body).json()
    client.post(f"/api/v1/jobs/{job['id']}/runs", headers=headers, json={"mode": "agent"})

    transcript = client.get(f"/api/v1/jobs/{job['id']}/transcript", headers=headers).json()
    for secret in [
        "arjun.mehta@example.com",
        "98765 43210",
        "ABCDE1234F",
        "4123 4567 8901",
        "4111 1111 1111 1111",
        "192.168.10.44",
    ]:
        assert secret not in transcript["text"], f"{secret} survived scrubbing"
    assert transcript["pii_redaction_count"] >= 6


@pytest.mark.skipif(not CORPUS, reason="run scripts/make_fixtures.py to generate the corpus")
def test_hedged_statements_score_lower_than_firm_ones(client, auth):
    """Confidence must be informative, or the reviewer cannot triage by it."""
    headers, _ = auth(Role.REVIEWER)
    body = (FIXTURES / "06_hedged_ambiguous.txt").read_text(encoding="utf-8")

    job = _upload_text(client, headers, "hedged.txt", body).json()
    client.post(f"/api/v1/jobs/{job['id']}/runs", headers=headers, json={"mode": "agent"})
    gate = client.get(f"/api/v1/jobs/{job['id']}/minutes", headers=headers).json()[
        "pending_approvals"
    ][0]

    items = gate["payload"]["decisions"] + gate["payload"]["actions"]
    hedged = [i for i in items if "maybe" in i["text"].lower() or "perhaps" in i["text"].lower()]
    firm = [i for i in items if "we have decided" in i["text"].lower()]

    if hedged and firm:
        assert max(i["confidence"] for i in hedged) < max(i["confidence"] for i in firm)
