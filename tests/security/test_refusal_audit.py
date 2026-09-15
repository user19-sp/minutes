"""Refusals must leave evidence (threat model T-08, repudiation).

Regression guard for a real bug. Every refusal path records an audit event and
then raises an HTTPException. The `get_db` dependency rolls back on any
exception, so under the original transactional-only audit writer the evidence of
a refusal was destroyed along with the request that was refused -- failed logins
and rejected uploads were silently never recorded.

Refusal events are now written on an independent session that commits
immediately. These tests assert that, because the threat model claims
"denials and refusals are recorded alongside successes" and that claim has to be
true.
"""

from __future__ import annotations

import io

import pytest

from backend.app.models import ActorType, AuditEvent, Role


def _events(db, action: str) -> list[AuditEvent]:
    return db.query(AuditEvent).filter(AuditEvent.action == action).all()


def test_failed_login_is_recorded(client, reviewer_factory, db):
    _, email, _ = reviewer_factory(Role.REVIEWER)

    resp = client.post("/api/v1/auth/login", json={"email": email, "password": "wrong-password"})
    assert resp.status_code == 401

    rows = _events(db, "auth.login_failed")
    assert rows, "a failed login left no evidence"
    assert rows[0].outcome == "denied"
    assert rows[0].detail["reason"] == "bad_password"
    assert rows[0].trace_id, "refusal events must stay correlatable to the request"


def test_login_attempt_on_an_unknown_account_is_recorded(client, db):
    resp = client.post(
        "/api/v1/auth/login", json={"email": "nobody@example.com", "password": "some-password"}
    )
    assert resp.status_code == 401

    rows = _events(db, "auth.login_failed")
    assert rows, "an attempt against an unknown account left no evidence"
    assert rows[0].detail["reason"] == "unknown_email"
    assert rows[0].actor_type is ActorType.SYSTEM


def test_rejected_upload_is_recorded(client, auth, db):
    headers, _ = auth(Role.REVIEWER)

    resp = client.post(
        "/api/v1/jobs",
        headers=headers,
        files={"file": ("payload.wav", io.BytesIO(b"MZ\x90\x00" + b"\x00" * 300), "audio/wav")},
        data={"title": "probe"},
    )
    assert resp.status_code == 400

    rows = _events(db, "job.upload_rejected")
    assert rows, "a rejected upload left no evidence"
    assert rows[0].detail["reason"] == "content_mismatch"
    assert rows[0].outcome == "denied"


def test_rate_limit_block_is_recorded(client, reviewer_factory, db):
    _, email, _ = reviewer_factory(Role.REVIEWER)
    for _ in range(7):
        client.post("/api/v1/auth/login", json={"email": email, "password": "wrong-password"})

    rows = _events(db, "security.rate_limited")
    assert rows, "a blocked brute-force attempt left no evidence"
    assert rows[0].detail["retry_after"] > 0


def test_forbidden_gate_decision_is_recorded(client, auth, uploaded_job, db, reviewer_factory):
    """Attempting to authorise beyond your role is exactly what must be recorded."""
    from backend.app.agent.audit import AuditLogger
    from backend.app.agent.gates import GateForbidden, decide_gate
    from backend.app.models import ApprovalRequest

    headers, _ = auth(Role.REVIEWER)
    job = uploaded_job(headers)
    client.post(f"/api/v1/jobs/{job['id']}/runs", headers=headers, json={"mode": "agent"})

    gate_id = client.get(f"/api/v1/jobs/{job['id']}/minutes", headers=headers).json()[
        "pending_approvals"
    ][0]["id"]

    viewer, _, _ = reviewer_factory(Role.VIEWER)
    viewer = db.merge(viewer)
    gate = db.get(ApprovalRequest, gate_id)

    with pytest.raises(GateForbidden):
        decide_gate(db, AuditLogger(db), gate=gate, user=viewer, decision="approved")
    db.rollback()  # the route would do this when turning the error into a 403

    rows = _events(db, "gate.decision_forbidden")
    assert rows, "an unauthorised approval attempt left no evidence"
    assert rows[0].detail["role"] == "viewer"


def test_successful_actions_still_commit_transactionally(client, auth, uploaded_job, db):
    """The independent path must not have broken normal auditing: a success and
    its evidence still succeed or fail together."""
    headers, _ = auth(Role.REVIEWER)
    job = uploaded_job(headers)

    rows = _events(db, "job.created")
    assert rows, "successful actions must still be audited"
    assert rows[0].resource_id == job["id"]
    assert rows[0].outcome == "success"


def test_a_rolled_back_operation_leaves_no_success_row(client, auth, db):
    """The original guarantee still holds: a failed write must not leave a
    misleading 'it happened' row."""
    headers, _ = auth(Role.REVIEWER)

    before = len(_events(db, "job.created"))
    client.post(
        "/api/v1/jobs",
        headers=headers,
        files={"file": ("bad.wav", io.BytesIO(b"MZ\x90\x00" + b"\x00" * 300), "audio/wav")},
        data={"title": "never stored"},
    )
    assert len(_events(db, "job.created")) == before, "a rejected upload logged a creation"
