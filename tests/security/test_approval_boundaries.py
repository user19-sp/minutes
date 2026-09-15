"""Human-approval-boundary tests (threat T-03).

The approval gate is the safety property the whole project rests on. These tests
attack it directly: replay a used approval, decide someone else's gate, decide
with an insufficient role, resume a run that was never approved, and escalate the
scope of an approved action.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from backend.app.agent import gates
from backend.app.agent.audit import AuditLogger
from backend.app.models import ApprovalStatus, Role


def _start_and_get_gate(client, headers, uploaded_job):
    job = uploaded_job(headers)
    run = client.post(
        f"/api/v1/jobs/{job['id']}/runs", headers=headers, json={"mode": "agent"}
    ).json()
    gate = client.get(f"/api/v1/jobs/{job['id']}/minutes", headers=headers).json()[
        "pending_approvals"
    ][0]
    return job, run, gate


def test_a_viewer_cannot_decide_a_gate(client, auth, uploaded_job):
    """Role boundary: viewers may read but never authorise."""
    reviewer_headers, _ = auth(Role.REVIEWER)
    viewer_headers, _ = auth(Role.VIEWER)

    job, _, gate = _start_and_get_gate(client, reviewer_headers, uploaded_job)

    resp = client.post(
        f"/api/v1/approvals/{gate['id']}/decide",
        headers=viewer_headers,
        json={"decision": "approved"},
    )
    # The viewer does not own the job either, so the gate is not even visible.
    assert resp.status_code in (403, 404)

    # The gate is untouched and nothing was written.
    still = client.get(f"/api/v1/approvals/{gate['id']}", headers=reviewer_headers).json()
    assert still["status"] == "pending"
    assert (
        client.get(f"/api/v1/jobs/{job['id']}/minutes", headers=reviewer_headers).json()[
            "action_items"
        ]
        == []
    )


def test_viewer_role_is_rejected_at_the_gate_layer(
    db, client, auth, uploaded_job, reviewer_factory
):
    """Direct unit check of the role rule, independent of route-level ownership."""
    headers, _owner = auth(Role.REVIEWER)
    _job, _run, gate_json = _start_and_get_gate(client, headers, uploaded_job)

    from backend.app.models import ApprovalRequest

    gate = db.get(ApprovalRequest, gate_json["id"])
    viewer, _, _ = reviewer_factory(Role.VIEWER)
    viewer = db.merge(viewer)

    with pytest.raises(gates.GateForbidden, match="may not decide approval gates"):
        gates.decide_gate(db, AuditLogger(db), gate=gate, user=viewer, decision="approved")

    assert gate.status is ApprovalStatus.PENDING


def test_an_approval_cannot_be_replayed(client, auth, uploaded_job):
    """A decided gate is terminal: approving twice must not authorise twice."""
    headers, _ = auth(Role.REVIEWER)
    job, _, gate = _start_and_get_gate(client, headers, uploaded_job)

    first = client.post(
        f"/api/v1/approvals/{gate['id']}/decide", headers=headers, json={"decision": "approved"}
    )
    assert first.status_code == 200

    replay = client.post(
        f"/api/v1/approvals/{gate['id']}/decide", headers=headers, json={"decision": "approved"}
    )
    assert replay.status_code == 409
    assert "cannot be re-decided or replayed" in replay.json()["detail"]

    # The write happened exactly once.
    minutes = client.get(f"/api/v1/jobs/{job['id']}/minutes", headers=headers).json()
    texts = [a["text"] for a in minutes["action_items"]]
    assert len(texts) == len(set(texts)), "replaying the approval duplicated the write"


def test_a_rejected_gate_cannot_later_be_approved(client, auth, uploaded_job):
    headers, _ = auth(Role.REVIEWER)
    job, _, gate = _start_and_get_gate(client, headers, uploaded_job)

    client.post(
        f"/api/v1/approvals/{gate['id']}/decide", headers=headers, json={"decision": "rejected"}
    )
    flip = client.post(
        f"/api/v1/approvals/{gate['id']}/decide", headers=headers, json={"decision": "approved"}
    )
    assert flip.status_code == 409
    assert (
        client.get(f"/api/v1/jobs/{job['id']}/minutes", headers=headers).json()["action_items"]
        == []
    )


def test_another_users_gate_is_invisible(client, auth, uploaded_job):
    """Cross-tenant: user B must not see or decide user A's gate (IDOR)."""
    headers_a, _ = auth(Role.REVIEWER)
    headers_b, _ = auth(Role.REVIEWER)
    _, _, gate = _start_and_get_gate(client, headers_a, uploaded_job)

    assert client.get(f"/api/v1/approvals/{gate['id']}", headers=headers_b).status_code == 404
    decide = client.post(
        f"/api/v1/approvals/{gate['id']}/decide", headers=headers_b, json={"decision": "approved"}
    )
    assert decide.status_code == 404, "must 404, not 403 -- do not confirm the id exists"

    queue_b = client.get("/api/v1/approvals", headers=headers_b).json()
    assert gate["id"] not in {g["id"] for g in queue_b}


def test_a_run_cannot_be_resumed_without_an_approval(client, auth, uploaded_job):
    """Resuming a suspended run without deciding its gate must not execute the write."""
    headers, _ = auth(Role.REVIEWER)
    job, run, _gate = _start_and_get_gate(client, headers, uploaded_job)

    resumed = client.post(f"/api/v1/runs/{run['id']}/resume", headers=headers)
    assert resumed.status_code == 200
    # No approved gate exists, so the run aborts rather than writing.
    assert resumed.json()["status"] == "aborted"
    assert (
        client.get(f"/api/v1/jobs/{job['id']}/minutes", headers=headers).json()["action_items"]
        == []
    )


def test_a_completed_run_cannot_be_resumed(client, auth, uploaded_job):
    headers, _ = auth(Role.REVIEWER)
    _job, run, gate = _start_and_get_gate(client, headers, uploaded_job)
    client.post(
        f"/api/v1/approvals/{gate['id']}/decide", headers=headers, json={"decision": "approved"}
    )

    again = client.post(f"/api/v1/runs/{run['id']}/resume", headers=headers)
    assert again.status_code == 409
    assert "only a suspended run can be resumed" in again.json()["detail"]


def test_export_scope_cannot_be_escalated_after_approval(client, auth, uploaded_job, db):
    """Approving 'approved items only' must not authorise 'everything'.

    Simulates a tampered gate payload: the stored payload is widened after the
    human ruled on it. The fingerprint no longer matches the grant, so the tool
    refuses to run.
    """
    from backend.app.models import ApprovalRequest

    headers, _ = auth(Role.REVIEWER)
    job, _, gate = _start_and_get_gate(client, headers, uploaded_job)
    client.post(
        f"/api/v1/approvals/{gate['id']}/decide", headers=headers, json={"decision": "approved"}
    )

    minutes = client.get(f"/api/v1/jobs/{job['id']}/minutes", headers=headers).json()
    for action in minutes["action_items"]:
        client.post(
            f"/api/v1/actions/{action['id']}/review",
            headers=headers,
            json={"status": "approved", "review_seconds": 3.0},
        )

    export = client.post(
        f"/api/v1/jobs/{job['id']}/exports",
        headers=headers,
        json={"format": "csv", "include_unapproved": False},
    ).json()
    export_gate_id = export["approval"]["id"]

    # Human approves the narrow request...
    client.post(
        f"/api/v1/approvals/{export_gate_id}/decide",
        headers=headers,
        json={"decision": "approved"},
        params={"resume": "false"},
    )

    # ...and an attacker widens the stored payload before the run resumes.
    row = db.get(ApprovalRequest, export_gate_id)
    row.payload = {**row.payload, "include_unapproved": True, "format": "json"}
    db.commit()

    resumed = client.post(f"/api/v1/runs/{export['run_id']}/resume", headers=headers)
    assert resumed.status_code == 200
    # The tampered payload no longer matches the approved fingerprint, so the
    # gated tool refuses and the run fails rather than exporting the wider set.
    assert resumed.json()["status"] == "failed"
    assert client.get(f"/api/v1/jobs/{job['id']}/exports", headers=headers).json() == []


def test_stale_gates_expire_rather_than_staying_open(client, auth, uploaded_job, db):
    from backend.app.models import ApprovalRequest

    headers, _ = auth(Role.REVIEWER)
    _, _, gate_json = _start_and_get_gate(client, headers, uploaded_job)

    row = db.get(ApprovalRequest, gate_json["id"])
    row.created_at = row.created_at - timedelta(days=5)
    db.commit()

    expired = gates.expire_stale_gates(db, AuditLogger(db))
    db.commit()
    assert expired == 1
    assert db.get(ApprovalRequest, gate_json["id"]).status is ApprovalStatus.EXPIRED

    # An expired gate grants nothing.
    assert gates.granted_keys_for_run(db, row.run_id) == set()


def test_every_gate_decision_names_the_decider(client, auth, uploaded_job):
    """Accountability: a ruling with no attributable human is not acceptable."""
    headers, user = auth(Role.REVIEWER)
    job, _, gate = _start_and_get_gate(client, headers, uploaded_job)

    client.post(
        f"/api/v1/approvals/{gate['id']}/decide",
        headers=headers,
        json={"decision": "approved", "note": "Verified against the recording."},
    )

    decided = client.get(f"/api/v1/approvals/{gate['id']}", headers=headers).json()
    assert decided["decided_by_id"] == user.id
    assert decided["decided_at"] is not None
    assert decided["decision_note"] == "Verified against the recording."

    trail = client.get(f"/api/v1/jobs/{job['id']}/audit", headers=headers).json()
    approvals = [e for e in trail if e["action"] == "gate.approved"]
    assert len(approvals) == 1
    assert approvals[0]["actor_type"] == "human"
    assert approvals[0]["actor_id"] == user.id
    assert approvals[0]["detail"]["decider_role"] == "reviewer"
