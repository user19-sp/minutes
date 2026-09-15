"""The full journey: upload -> run -> gate -> review -> gated export -> download.

This is the integration test the acceptance-gate checklist calls for ("a genuine
pipeline exists"). It asserts the governance properties at every hop, not just
that the happy path returns 200.
"""

from __future__ import annotations

from backend.app.models import Role


def test_full_pipeline_upload_to_export(client, auth, uploaded_job):
    headers, _user = auth(Role.REVIEWER)

    # --- 1. upload ------------------------------------------------------- #
    job = uploaded_job(headers)
    assert job["status"] == "uploaded"
    assert len(job["sha256"]) == 64

    # --- 2. run the governed pipeline ------------------------------------ #
    run = client.post(
        f"/api/v1/jobs/{job['id']}/runs",
        headers=headers,
        json={"mode": "agent", "enable_diarization": True},
    )
    assert run.status_code == 201, run.text
    run_body = run.json()

    # The run must stop at the write gate rather than persisting on its own.
    assert run_body["status"] == "awaiting_approval"
    assert run_body["tool_call_count"] >= 4  # transcribe, diarize, segment, extract
    assert run_body["denied_tool_call_count"] == 0

    # --- 3. transcript was stored, scrubbed and segmented ----------------- #
    transcript = client.get(f"/api/v1/jobs/{job['id']}/transcript", headers=headers).json()
    assert "sprint review" in transcript["text"].lower()
    assert transcript["diarization_enabled"] is True
    assert any(s["speaker"] for s in transcript["segments"])

    minutes = client.get(f"/api/v1/jobs/{job['id']}/minutes", headers=headers).json()
    assert len(minutes["agenda_blocks"]) >= 2
    # Nothing is written to the record before the gate is approved.
    assert minutes["decisions"] == []
    assert minutes["action_items"] == []
    assert len(minutes["pending_approvals"]) == 1

    gate = minutes["pending_approvals"][0]
    assert gate["action"] == "persist_minutes"
    assert gate["status"] == "pending"
    assert gate["payload"]["decisions"], "the gate must show the reviewer what will be written"

    # --- 4. approve the write gate --------------------------------------- #
    decided = client.post(
        f"/api/v1/approvals/{gate['id']}/decide",
        headers=headers,
        json={"decision": "approved", "note": "Checked against the recording."},
    )
    assert decided.status_code == 200, decided.text
    assert decided.json()["status"] == "approved"

    minutes = client.get(f"/api/v1/jobs/{job['id']}/minutes", headers=headers).json()
    assert minutes["decisions"], "approving the gate must persist the drafts"
    assert minutes["action_items"]
    assert minutes["latest_run"]["status"] == "completed"
    assert all(d["status"] == "proposed" for d in minutes["decisions"])

    # Every item is traceable to the extractor and the run that produced it.
    for item in minutes["action_items"]:
        assert item["source_tool"].startswith("baseline-extractor")
        assert item["run_id"] == run_body["id"]
        assert item["evidence_quote"]

    # --- 5. export before review is refused outright --------------------- #
    premature = client.post(
        f"/api/v1/jobs/{job['id']}/exports", headers=headers, json={"format": "csv"}
    )
    assert premature.status_code == 409, premature.text
    assert "no reviewer-approved action items" in premature.json()["detail"]
    # Refused before a gate was opened, so no reviewer was asked to rubber-stamp it.
    pending = client.get("/api/v1/approvals", headers=headers).json()
    assert not any(g["action"] == "export_actions" for g in pending)
    assert client.get(f"/api/v1/jobs/{job['id']}/exports", headers=headers).json() == []

    # --- 6. review the items --------------------------------------------- #
    first_action = minutes["action_items"][0]
    edited = client.post(
        f"/api/v1/actions/{first_action['id']}/review",
        headers=headers,
        json={
            "status": "edited",
            "text": "Priya to send the updated API contract to the vendor.",
            "owner_name": "Priya",
            "deadline": "2026-09-19",
            "review_seconds": 14.5,
        },
    )
    assert edited.status_code == 200, edited.text
    body = edited.json()
    assert body["status"] == "edited"
    assert body["owner_name"] == "Priya"
    # The model's original proposal is preserved for error analysis.
    assert body["original_text"] == first_action["text"]
    assert body["review_seconds"] == 14.5

    for action in minutes["action_items"][1:]:
        client.post(
            f"/api/v1/actions/{action['id']}/review",
            headers=headers,
            json={"status": "approved", "review_seconds": 4.0},
        )
    for decision in minutes["decisions"]:
        client.post(
            f"/api/v1/decisions/{decision['id']}/review",
            headers=headers,
            json={"status": "approved", "review_seconds": 6.0},
        )

    stats = client.get(f"/api/v1/jobs/{job['id']}/review-stats", headers=headers).json()
    assert stats["action_items"]["reviewed"] == len(minutes["action_items"])
    assert stats["action_items"]["mean_review_seconds"] is not None

    # --- 7. export now opens a second gate ------------------------------- #
    export_req = client.post(
        f"/api/v1/jobs/{job['id']}/exports", headers=headers, json={"format": "csv"}
    )
    assert export_req.status_code == 202, export_req.text
    export_body = export_req.json()
    assert export_body["status"] == "awaiting_approval"
    export_gate = export_body["approval"]
    assert export_gate["action"] == "export_actions"
    assert export_gate["risk"] == "high"

    # Still nothing on disk.
    assert client.get(f"/api/v1/jobs/{job['id']}/exports", headers=headers).json() == []

    # --- 8. approve the export ------------------------------------------- #
    approved = client.post(
        f"/api/v1/approvals/{export_gate['id']}/decide",
        headers=headers,
        json={"decision": "approved"},
    )
    assert approved.status_code == 200, approved.text

    exports = client.get(f"/api/v1/jobs/{job['id']}/exports", headers=headers).json()
    assert len(exports) == 1
    assert exports[0]["item_count"] >= 1
    assert len(exports[0]["sha256"]) == 64

    # --- 9. download ------------------------------------------------------ #
    download = client.get(f"/api/v1/exports/{exports[0]['id']}/download", headers=headers)
    assert download.status_code == 200
    csv_text = download.text
    assert "Priya to send the updated API contract" in csv_text
    assert "action_id" in csv_text.splitlines()[0]

    # --- 10. the whole journey is on the audit trail ---------------------- #
    trail = client.get(f"/api/v1/jobs/{job['id']}/audit", headers=headers).json()
    actions = {e["action"] for e in trail}
    for expected in {
        "job.created",
        "run.started",
        "tool.call",
        "tool.approval_required",
        "gate.opened",
        "gate.approved",
        "run.resumed",
        "pipeline.transcript_stored",
        "review.action_item.edited",
        "export.requested",
        "export.downloaded",
    }:
        assert expected in actions, f"missing audit action: {expected}"

    # Every event is attributable and correlated.
    assert all(e["actor_type"] in ("human", "agent", "system") for e in trail)
    assert all(e["trace_id"] for e in trail)


def test_run_trace_is_reconstructable(client, auth, uploaded_job):
    """A reviewer can replay exactly what the agent did, step by step."""
    headers, _ = auth(Role.REVIEWER)
    job = uploaded_job(headers)
    run = client.post(
        f"/api/v1/jobs/{job['id']}/runs", headers=headers, json={"mode": "agent"}
    ).json()

    trace = client.get(f"/api/v1/runs/{run['id']}/trace", headers=headers).json()
    tools_called = [step["tool"] for step in trace["steps"]]
    assert tools_called == ["transcribe_audio", "segment_agenda", "extract_decisions_actions"]
    assert all(step["outcome"] == "allowed" for step in trace["steps"])
    assert all("duration_ms" in step for step in trace["steps"])


def test_rejecting_the_gate_writes_nothing(client, auth, uploaded_job):
    """The human said no. Nothing is persisted and the run aborts cleanly."""
    headers, _ = auth(Role.REVIEWER)
    job = uploaded_job(headers)
    client.post(f"/api/v1/jobs/{job['id']}/runs", headers=headers, json={"mode": "agent"})

    gate = client.get(f"/api/v1/jobs/{job['id']}/minutes", headers=headers).json()[
        "pending_approvals"
    ][0]
    client.post(
        f"/api/v1/approvals/{gate['id']}/decide",
        headers=headers,
        json={"decision": "rejected", "note": "Transcript quality too poor to trust."},
    )

    minutes = client.get(f"/api/v1/jobs/{job['id']}/minutes", headers=headers).json()
    assert minutes["decisions"] == []
    assert minutes["action_items"] == []
    assert minutes["latest_run"]["status"] == "aborted"

    trail = {
        e["action"] for e in client.get(f"/api/v1/jobs/{job['id']}/audit", headers=headers).json()
    }
    assert "gate.rejected" in trail
    assert "run.aborted_no_approval" in trail
