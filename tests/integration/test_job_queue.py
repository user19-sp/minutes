"""Job queue (blueprint §2.1 module 1).

A run used to execute inside the HTTP request. That works while the baseline STT
is instant, but Whisper on a 30-minute recording takes minutes and nginx closes
the connection at 300s. These tests pin the queued path: the request returns
immediately, a worker does the work, and nothing is lost if the worker dies.
"""

from __future__ import annotations

import threading
from datetime import timedelta

import pytest

from backend.app.agent.audit import AuditLogger
from backend.app.config import settings
from backend.app.models import AgentRun, JobStatus, Role, RunMode, RunStatus
from backend.app.services import queue


@pytest.fixture
def queued_mode(monkeypatch):
    """Switch the API to the deployment execution mode for one test."""
    monkeypatch.setattr(settings, "run_execution", "queued")
    yield


# --------------------------------------------------------------------------- #
# Enqueue
# --------------------------------------------------------------------------- #


def test_run_request_returns_immediately_without_doing_the_work(
    client, auth, uploaded_job, queued_mode
):
    """The whole point: the caller is not held open while the pipeline runs."""
    headers, _ = auth(Role.REVIEWER)
    job = uploaded_job(headers)

    resp = client.post(f"/api/v1/jobs/{job['id']}/runs", headers=headers, json={"mode": "agent"})

    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "queued"
    assert body["tool_call_count"] == 0, "no tool ran inside the request"

    # Nothing has been transcribed or extracted yet.
    minutes = client.get(f"/api/v1/jobs/{job['id']}/minutes", headers=headers).json()
    assert minutes["transcript"] is None
    assert minutes["action_items"] == []
    assert minutes["job"]["status"] == "queued"


def test_queueing_is_audited(client, auth, uploaded_job, queued_mode, db):
    headers, _ = auth(Role.REVIEWER)
    job = uploaded_job(headers)
    client.post(f"/api/v1/jobs/{job['id']}/runs", headers=headers, json={"mode": "agent"})

    trail = {
        e["action"] for e in client.get(f"/api/v1/jobs/{job['id']}/audit", headers=headers).json()
    }
    assert "run.queued" in trail


def test_a_queued_job_cannot_be_queued_twice(client, auth, uploaded_job, queued_mode):
    """Otherwise one meeting gets two runs and two approval gates."""
    headers, _ = auth(Role.REVIEWER)
    job = uploaded_job(headers)

    first = client.post(f"/api/v1/jobs/{job['id']}/runs", headers=headers, json={"mode": "agent"})
    assert first.status_code == 202

    second = client.post(f"/api/v1/jobs/{job['id']}/runs", headers=headers, json={"mode": "agent"})
    assert second.status_code == 409
    assert "queued" in second.json()["detail"]


# --------------------------------------------------------------------------- #
# Claim
# --------------------------------------------------------------------------- #


def test_worker_claims_and_executes_a_queued_run(client, auth, uploaded_job, queued_mode, db):
    """End to end through the queue: enqueue, claim, execute, reach the gate."""
    headers, _ = auth(Role.REVIEWER)
    job = uploaded_job(headers)
    client.post(f"/api/v1/jobs/{job['id']}/runs", headers=headers, json={"mode": "agent"})

    run = queue.claim_next(db, "test-worker")
    assert run is not None
    assert run.status is RunStatus.RUNNING
    assert run.claimed_by == "test-worker"
    assert run.attempts == 1

    queue.execute_claimed(db, AuditLogger(db), run)

    # Same outcome as an inline run: suspended at the write gate.
    minutes = client.get(f"/api/v1/jobs/{job['id']}/minutes", headers=headers).json()
    assert minutes["latest_run"]["status"] == "awaiting_approval"
    assert len(minutes["pending_approvals"]) == 1
    assert minutes["transcript"] is not None


def test_claim_returns_none_on_an_empty_queue(db):
    assert queue.claim_next(db, "idle-worker") is None


def test_a_run_is_claimed_exactly_once(client, auth, uploaded_job, queued_mode, db):
    """Two workers must never take the same run -- that would run the pipeline
    twice and open two gates for one meeting."""
    headers, _ = auth(Role.REVIEWER)
    job = uploaded_job(headers)
    client.post(f"/api/v1/jobs/{job['id']}/runs", headers=headers, json={"mode": "agent"})

    first = queue.claim_next(db, "worker-a")
    second = queue.claim_next(db, "worker-b")

    assert first is not None
    assert second is None, "the same run was handed to two workers"


def test_runs_are_claimed_oldest_first(client, auth, uploaded_job, queued_mode, db):
    headers, _ = auth(Role.REVIEWER)
    first_job = uploaded_job(headers, title="first")
    second_job = uploaded_job(headers, title="second")

    client.post(f"/api/v1/jobs/{first_job['id']}/runs", headers=headers, json={"mode": "agent"})
    client.post(f"/api/v1/jobs/{second_job['id']}/runs", headers=headers, json={"mode": "agent"})

    claimed = queue.claim_next(db, "w")
    assert claimed.job_id == first_job["id"], "queue is not FIFO"


# --------------------------------------------------------------------------- #
# Recovery
# --------------------------------------------------------------------------- #


def test_a_run_abandoned_by_a_dead_worker_is_requeued(client, auth, uploaded_job, queued_mode, db):
    """A crashed worker must not strand a meeting in RUNNING forever."""
    headers, _ = auth(Role.REVIEWER)
    job = uploaded_job(headers)
    client.post(f"/api/v1/jobs/{job['id']}/runs", headers=headers, json={"mode": "agent"})

    run = queue.claim_next(db, "worker-that-dies")
    run.claimed_at = run.claimed_at - timedelta(hours=2)  # lease long expired
    db.commit()

    recovered = queue.requeue_abandoned(db, AuditLogger(db))
    assert recovered == 1

    db.refresh(run)
    assert run.status is RunStatus.QUEUED
    assert run.claimed_by is None

    # And another worker can now pick it up.
    assert queue.claim_next(db, "worker-b") is not None


def test_a_healthy_in_flight_run_is_not_requeued(client, auth, uploaded_job, queued_mode, db):
    """Recovery must not steal work from a worker that is simply still busy."""
    headers, _ = auth(Role.REVIEWER)
    job = uploaded_job(headers)
    client.post(f"/api/v1/jobs/{job['id']}/runs", headers=headers, json={"mode": "agent"})

    run = queue.claim_next(db, "busy-worker")
    assert queue.requeue_abandoned(db, AuditLogger(db)) == 0

    db.refresh(run)
    assert run.status is RunStatus.RUNNING
    assert run.claimed_by == "busy-worker"


def test_a_poison_run_eventually_fails_instead_of_looping(
    client, auth, uploaded_job, queued_mode, db
):
    """A job that kills every worker must not occupy the queue forever."""
    headers, _ = auth(Role.REVIEWER)
    job = uploaded_job(headers)
    client.post(f"/api/v1/jobs/{job['id']}/runs", headers=headers, json={"mode": "agent"})

    run = db.query(AgentRun).filter(AgentRun.status == RunStatus.QUEUED).one()
    run.status = RunStatus.RUNNING
    run.claimed_by = "doomed"
    run.claimed_at = queue._now() - timedelta(hours=2)
    run.attempts = queue.MAX_ATTEMPTS
    db.commit()

    queue.requeue_abandoned(db, AuditLogger(db))
    db.refresh(run)

    assert run.status is RunStatus.FAILED
    assert "giving up" in run.error_message

    job_after = client.get(f"/api/v1/jobs/{job['id']}", headers=headers).json()
    assert job_after["status"] == "failed"
    assert job_after["error_message"]


# --------------------------------------------------------------------------- #
# Worker loop and visibility
# --------------------------------------------------------------------------- #


def test_worker_loop_drains_the_queue(client, auth, uploaded_job, queued_mode, db):
    """The actual loop the worker container runs."""
    headers, _ = auth(Role.REVIEWER)
    job = uploaded_job(headers)
    client.post(f"/api/v1/jobs/{job['id']}/runs", headers=headers, json={"mode": "agent"})

    stop = threading.Event()
    worker = threading.Thread(target=queue.run_worker, kwargs={"stop": stop, "poll": 0.05})
    worker.start()
    try:
        for _ in range(100):  # up to ~5s
            status = client.get(f"/api/v1/jobs/{job['id']}", headers=headers).json()["status"]
            if status == "awaiting_approval":
                break
            stop.wait(0.05)
    finally:
        stop.set()
        worker.join(timeout=10)

    assert status == "awaiting_approval", f"worker did not drain the queue (job is {status})"


def test_queue_endpoint_reports_depth_and_in_flight(client, auth, uploaded_job, queued_mode, db):
    headers, _ = auth(Role.REVIEWER)
    job = uploaded_job(headers)
    client.post(f"/api/v1/jobs/{job['id']}/runs", headers=headers, json={"mode": "agent"})

    view = client.get("/api/v1/queue", headers=headers).json()
    assert view["execution_mode"] == "queued"
    assert view["queued"] == 1
    assert view["running"] == []

    queue.claim_next(db, "worker-a")
    view = client.get("/api/v1/queue", headers=headers).json()
    assert view["queued"] == 0
    assert len(view["running"]) == 1
    assert view["running"][0]["worker"] == "worker-a"


def test_inline_mode_still_works(client, auth, uploaded_job):
    """The dev/test path must keep returning a finished run, or every existing
    test would have to learn to poll."""
    headers, _ = auth(Role.REVIEWER)
    job = uploaded_job(headers)

    resp = client.post(f"/api/v1/jobs/{job['id']}/runs", headers=headers, json={"mode": "agent"})
    assert resp.status_code == 201
    assert resp.json()["status"] == "awaiting_approval"


def test_queued_and_inline_reach_the_same_outcome(client, auth, uploaded_job, monkeypatch, db):
    """Execution mode is transport. It must not change what the pipeline produces."""
    headers, _ = auth(Role.REVIEWER)

    inline_job = uploaded_job(headers, title="inline")
    client.post(f"/api/v1/jobs/{inline_job['id']}/runs", headers=headers, json={"mode": "agent"})
    inline = client.get(f"/api/v1/jobs/{inline_job['id']}/minutes", headers=headers).json()

    monkeypatch.setattr(settings, "run_execution", "queued")
    queued_job = uploaded_job(headers, title="queued")
    client.post(f"/api/v1/jobs/{queued_job['id']}/runs", headers=headers, json={"mode": "agent"})
    run = queue.claim_next(db, "w")
    queue.execute_claimed(db, AuditLogger(db), run)
    queued = client.get(f"/api/v1/jobs/{queued_job['id']}/minutes", headers=headers).json()

    assert [d["text"] for d in inline["pending_approvals"][0]["payload"]["decisions"]] == [
        d["text"] for d in queued["pending_approvals"][0]["payload"]["decisions"]
    ]


def test_diarization_choice_survives_the_queue(client, auth, uploaded_job, queued_mode, db):
    """The run parameters are carried on the row, not held in the request."""
    headers, _ = auth(Role.REVIEWER)
    job = uploaded_job(headers)
    client.post(
        f"/api/v1/jobs/{job['id']}/runs",
        headers=headers,
        json={"mode": "agent", "enable_diarization": True},
    )

    run = queue.claim_next(db, "w")
    assert run.enable_diarization is True
    queue.execute_claimed(db, AuditLogger(db), run)

    transcript = client.get(f"/api/v1/jobs/{job['id']}/transcript", headers=headers).json()
    assert transcript["diarization_enabled"] is True
    assert any(s["speaker"] for s in transcript["segments"])


def test_control_arm_also_goes_through_the_queue(client, auth, uploaded_job, queued_mode, db):
    headers, _ = auth(Role.REVIEWER)
    job = uploaded_job(headers)
    client.post(f"/api/v1/jobs/{job['id']}/runs", headers=headers, json={"mode": "no_agent"})

    run = queue.claim_next(db, "w")
    assert run.mode is RunMode.NO_AGENT
    queue.execute_claimed(db, AuditLogger(db), run)

    db.refresh(run)
    assert run.status is RunStatus.COMPLETED
    minutes = client.get(f"/api/v1/jobs/{job['id']}/minutes", headers=headers).json()
    assert minutes["action_items"], "the control arm still writes straight through"
    assert minutes["job"]["status"] in (JobStatus.READY_FOR_REVIEW, "ready_for_review")
