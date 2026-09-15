"""Sensitive-data-leakage tests (threat T-04).

Four places data can escape: the database, the audit trail, API responses, and
export files. Each is tested here.
"""

from __future__ import annotations

import pytest

from backend.app.models import Role
from backend.app.security.pii import detect, scrub_text

PII_TRANSCRIPT = """Let us start the onboarding call.
Send the offer letter to priya.sharma@example.com and copy me.
Her contact number is +91 98765 43210 for any clarification.
The PAN on file is ABCDE1234F and the Aadhaar is 4123 4567 8901.
Payment will go to card 4111 1111 1111 1111 as discussed.
The staging box is at 192.168.10.44 if anyone needs it.
Rahul will update the HR system by Friday.
"""


# --------------------------------------------------------------------------- #
# Unit: the scrubber itself
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "kind,sample",
    [
        ("EMAIL", "reach me at priya.sharma@example.com please"),
        ("PHONE", "call +91 98765 43210 tomorrow"),
        ("PAN", "the PAN is ABCDE1234F on file"),
        ("AADHAAR", "aadhaar 4123 4567 8901 verified"),
        ("CARD", "card 4111 1111 1111 1111 was charged"),
        ("IP", "server at 192.168.10.44 is down"),
    ],
)
def test_each_identifier_class_is_redacted(kind: str, sample: str):
    result = scrub_text(sample)
    assert result.count >= 1, f"{kind} not detected in {sample!r}"
    assert f"[REDACTED:{kind}]" in result.text or kind in result.kinds
    # The raw value must not survive anywhere in the output.
    for finding in result.findings:
        assert finding.value not in result.text


def test_scrubbing_is_not_reversible():
    """No mapping back to the original is stored anywhere in the output."""
    result = scrub_text("email priya.sharma@example.com now")
    assert "priya.sharma" not in result.text
    assert "example.com" not in result.text


def test_ordinary_numbers_are_not_over_redacted():
    """A 16-digit order number that fails the Luhn check is not a card."""
    text = "Order 1234567890123456 shipped and ticket 42 was closed."
    findings = {f.kind for f in detect(text)}
    assert "CARD" not in findings


def test_scrubbing_preserves_the_surrounding_sentence():
    result = scrub_text("Send the offer to priya.sharma@example.com by Friday.")
    assert result.text.startswith("Send the offer to ")
    assert result.text.endswith(" by Friday.")


# --------------------------------------------------------------------------- #
# Integration: nothing sensitive reaches storage, responses or exports
# --------------------------------------------------------------------------- #


def test_pii_never_reaches_the_database(client, auth, uploaded_job, db):
    """Scrubbing happens at the storage boundary, so a DB dump holds no identifiers."""
    headers, _ = auth(Role.REVIEWER)
    job = uploaded_job(headers, transcript=PII_TRANSCRIPT, title="Onboarding")
    client.post(f"/api/v1/jobs/{job['id']}/runs", headers=headers, json={"mode": "agent"})

    from backend.app.models import Transcript

    row = db.query(Transcript).filter(Transcript.job_id == job["id"]).one()

    for secret in [
        "priya.sharma@example.com",
        "98765 43210",
        "ABCDE1234F",
        "4123 4567 8901",
        "4111 1111 1111 1111",
        "192.168.10.44",
    ]:
        assert secret not in row.text, f"{secret!r} was persisted unredacted"

    assert row.pii_redaction_count >= 6
    assert "[REDACTED:" in row.text
    # The non-sensitive content is intact -- scrubbing must not destroy the minutes.
    assert "Rahul will update the HR system" in row.text

    # Diarized segments are scrubbed too, not just the flat text.
    for seg in row.segments:
        assert "priya.sharma@example.com" not in seg["text"]


def test_pii_does_not_leak_through_the_api(client, auth, uploaded_job):
    headers, _ = auth(Role.REVIEWER)
    job = uploaded_job(headers, transcript=PII_TRANSCRIPT)
    client.post(f"/api/v1/jobs/{job['id']}/runs", headers=headers, json={"mode": "agent"})

    for path in (
        f"/api/v1/jobs/{job['id']}/transcript",
        f"/api/v1/jobs/{job['id']}/minutes",
        f"/api/v1/jobs/{job['id']}/audit",
    ):
        body = client.get(path, headers=headers).text
        assert "priya.sharma@example.com" not in body, f"{path} leaked an email"
        assert "ABCDE1234F" not in body, f"{path} leaked a PAN"


def test_audit_trail_never_records_secrets(client, auth, uploaded_job):
    """Audit payloads are sanitised: credentials and tokens are stripped."""
    headers, _ = auth(Role.REVIEWER)
    job = uploaded_job(headers)
    client.post(f"/api/v1/jobs/{job['id']}/runs", headers=headers, json={"mode": "agent"})

    trail = client.get(f"/api/v1/jobs/{job['id']}/audit", headers=headers).text
    for forbidden in ("hashed_password", "$2b$", "Bearer ", "jwt_secret"):
        assert forbidden not in trail


def test_login_response_never_contains_the_password_hash(client, reviewer_factory):
    _, email, password = reviewer_factory(Role.REVIEWER)
    body = client.post("/api/v1/auth/login", json={"email": email, "password": password}).json()
    assert "hashed_password" not in body["user"]
    assert "$2b$" not in str(body)


def test_login_does_not_reveal_whether_an_account_exists(client, reviewer_factory):
    """Account enumeration: both failure modes return the same status and message."""
    _, email, _password = reviewer_factory(Role.REVIEWER)

    unknown = client.post(
        "/api/v1/auth/login", json={"email": "nobody@example.com", "password": "whatever-long-pw"}
    )
    wrong_pw = client.post(
        "/api/v1/auth/login", json={"email": email, "password": "definitely-wrong-pw"}
    )

    assert unknown.status_code == wrong_pw.status_code == 401
    assert unknown.json()["detail"] == wrong_pw.json()["detail"]


def test_export_contains_only_reviewer_approved_items(client, auth, uploaded_job):
    """A rejected item must never leave the system."""
    headers, _ = auth(Role.REVIEWER)
    job = uploaded_job(headers)
    client.post(f"/api/v1/jobs/{job['id']}/runs", headers=headers, json={"mode": "agent"})

    gate = client.get(f"/api/v1/jobs/{job['id']}/minutes", headers=headers).json()[
        "pending_approvals"
    ][0]
    client.post(
        f"/api/v1/approvals/{gate['id']}/decide", headers=headers, json={"decision": "approved"}
    )

    minutes = client.get(f"/api/v1/jobs/{job['id']}/minutes", headers=headers).json()
    actions = minutes["action_items"]
    assert len(actions) >= 2

    rejected_text = actions[0]["text"]
    client.post(
        f"/api/v1/actions/{actions[0]['id']}/review",
        headers=headers,
        json={"status": "rejected", "note": "Not an action item.", "review_seconds": 2.0},
    )
    kept_text = actions[1]["text"]
    client.post(
        f"/api/v1/actions/{actions[1]['id']}/review",
        headers=headers,
        json={"status": "approved", "review_seconds": 2.0},
    )
    # actions[2:] are left un-reviewed on purpose.

    export = client.post(
        f"/api/v1/jobs/{job['id']}/exports", headers=headers, json={"format": "csv"}
    ).json()
    client.post(
        f"/api/v1/approvals/{export['approval']['id']}/decide",
        headers=headers,
        json={"decision": "approved"},
    )

    record = client.get(f"/api/v1/jobs/{job['id']}/exports", headers=headers).json()[0]
    csv_text = client.get(f"/api/v1/exports/{record['id']}/download", headers=headers).text

    assert kept_text in csv_text
    assert rejected_text not in csv_text, "a rejected item was exported"
    assert record["item_count"] == 1, "un-reviewed items must not be exported by default"


def test_csv_export_neutralises_formula_injection(client, auth, uploaded_job):
    """An action item beginning with '=' must not execute when opened in Excel."""
    from backend.app.services.export import neutralise_csv

    assert neutralise_csv("=cmd|'/c calc'!A1").startswith("'=")
    assert neutralise_csv("+1234").startswith("'+")
    assert neutralise_csv("-SUM(A1)").startswith("'-")
    assert neutralise_csv("@import").startswith("'@")
    assert neutralise_csv("Normal task text") == "Normal task text"

    # End to end: a hostile item text survives review and is neutralised on export.
    headers, _ = auth(Role.REVIEWER)
    job = uploaded_job(headers)
    client.post(f"/api/v1/jobs/{job['id']}/runs", headers=headers, json={"mode": "agent"})
    gate = client.get(f"/api/v1/jobs/{job['id']}/minutes", headers=headers).json()[
        "pending_approvals"
    ][0]
    client.post(
        f"/api/v1/approvals/{gate['id']}/decide", headers=headers, json={"decision": "approved"}
    )

    action = client.get(f"/api/v1/jobs/{job['id']}/minutes", headers=headers).json()[
        "action_items"
    ][0]
    client.post(
        f"/api/v1/actions/{action['id']}/review",
        headers=headers,
        json={"status": "edited", "text": '=HYPERLINK("http://evil.example","click")'},
    )

    export = client.post(
        f"/api/v1/jobs/{job['id']}/exports", headers=headers, json={"format": "csv"}
    ).json()
    client.post(
        f"/api/v1/approvals/{export['approval']['id']}/decide",
        headers=headers,
        json={"decision": "approved"},
    )
    record = client.get(f"/api/v1/jobs/{job['id']}/exports", headers=headers).json()[0]
    csv_text = client.get(f"/api/v1/exports/{record['id']}/download", headers=headers).text

    assert "\"'=HYPERLINK" in csv_text, "formula was not neutralised"


def test_another_users_data_is_not_reachable(client, auth, uploaded_job):
    """Cross-tenant isolation across every job-scoped route (IDOR)."""
    headers_a, _ = auth(Role.REVIEWER)
    headers_b, _ = auth(Role.REVIEWER)
    job = uploaded_job(headers_a)
    client.post(f"/api/v1/jobs/{job['id']}/runs", headers=headers_a, json={"mode": "agent"})

    for path in (
        f"/api/v1/jobs/{job['id']}",
        f"/api/v1/jobs/{job['id']}/transcript",
        f"/api/v1/jobs/{job['id']}/minutes",
        f"/api/v1/jobs/{job['id']}/audit",
        f"/api/v1/jobs/{job['id']}/runs",
        f"/api/v1/jobs/{job['id']}/exports",
    ):
        resp = client.get(path, headers=headers_b)
        assert resp.status_code == 404, f"{path} leaked another user's data ({resp.status_code})"

    assert client.get("/api/v1/jobs", headers=headers_b).json() == []


def test_global_audit_trail_is_admin_only(client, auth):
    reviewer_headers, _ = auth(Role.REVIEWER)
    admin_headers, _ = auth(Role.ADMIN)

    assert client.get("/api/v1/audit", headers=reviewer_headers).status_code == 403
    assert client.get("/api/v1/audit", headers=admin_headers).status_code == 200
