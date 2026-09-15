"""The agent-vs-no-agent comparison study (acceptance gate requirement).

Both arms run the same ML models over the same transcript. The only difference is
the governance layer. These tests establish what the report claims:

  * the ungoverned arm writes to the record with no human in the loop;
  * the governed arm cannot, and produces a step-by-step trace the other lacks;
  * governance costs measurable overhead, and that cost is what buys the control.
"""

from __future__ import annotations

from backend.app.models import Role


def _run(client, headers, job_id: str, mode: str) -> dict:
    return client.post(f"/api/v1/jobs/{job_id}/runs", headers=headers, json={"mode": mode}).json()


def test_control_arm_writes_without_any_human_approval(client, auth, uploaded_job):
    """This is the behaviour the governance layer exists to prevent."""
    headers, _ = auth(Role.REVIEWER)
    job = uploaded_job(headers)

    run = _run(client, headers, job["id"], "no_agent")
    assert run["status"] == "completed", "the control arm runs straight through"

    minutes = client.get(f"/api/v1/jobs/{job['id']}/minutes", headers=headers).json()
    assert minutes["action_items"], "the control arm wrote to the record"
    assert minutes["pending_approvals"] == [], "and asked nobody"

    # No gate was ever opened for this run.
    trail = {
        e["action"] for e in client.get(f"/api/v1/jobs/{job['id']}/audit", headers=headers).json()
    }
    assert "gate.opened" not in trail
    assert "run.ungoverned_control_arm" in trail, "the control arm must be labelled as such"


def test_governed_arm_cannot_write_without_approval(client, auth, uploaded_job):
    headers, _ = auth(Role.REVIEWER)
    job = uploaded_job(headers)

    run = _run(client, headers, job["id"], "agent")
    assert run["status"] == "awaiting_approval"

    minutes = client.get(f"/api/v1/jobs/{job['id']}/minutes", headers=headers).json()
    assert minutes["action_items"] == []
    assert len(minutes["pending_approvals"]) == 1


def test_both_arms_extract_the_same_items(client, auth, uploaded_job):
    """Like-for-like: the models are identical, so the extraction must match.

    If these ever diverge the comparison is measuring two different pipelines and
    the study is invalid -- so this test guards the study's premise.
    """
    headers, _ = auth(Role.REVIEWER)

    control_job = uploaded_job(headers, title="control")
    _run(client, headers, control_job["id"], "no_agent")
    control = client.get(f"/api/v1/jobs/{control_job['id']}/minutes", headers=headers).json()

    agent_job = uploaded_job(headers, title="agent")
    _run(client, headers, agent_job["id"], "agent")
    gate = client.get(f"/api/v1/jobs/{agent_job['id']}/minutes", headers=headers).json()[
        "pending_approvals"
    ][0]
    client.post(
        f"/api/v1/approvals/{gate['id']}/decide", headers=headers, json={"decision": "approved"}
    )
    governed = client.get(f"/api/v1/jobs/{agent_job['id']}/minutes", headers=headers).json()

    assert [a["text"] for a in control["action_items"]] == [
        a["text"] for a in governed["action_items"]
    ]
    assert [d["text"] for d in control["decisions"]] == [d["text"] for d in governed["decisions"]]


def test_only_the_governed_arm_produces_a_tool_trace(client, auth, uploaded_job):
    """Traceability is the governed arm's distinguishing artefact."""
    headers, _ = auth(Role.REVIEWER)

    agent_job = uploaded_job(headers)
    agent_run = _run(client, headers, agent_job["id"], "agent")
    agent_trace = client.get(f"/api/v1/runs/{agent_run['id']}/trace", headers=headers).json()

    control_job = uploaded_job(headers)
    control_run = _run(client, headers, control_job["id"], "no_agent")
    control_trace = client.get(f"/api/v1/runs/{control_run['id']}/trace", headers=headers).json()

    assert all("tool" in step for step in agent_trace["steps"])
    assert agent_trace["tool_calls"] >= 3
    # The control arm records stages but no governed tool calls.
    assert control_trace["tool_calls"] == 0
    assert all("governed" in step and step["governed"] is False for step in control_trace["steps"])


def test_comparison_endpoint_reports_both_arms(client, auth, uploaded_job):
    """The figures the report's comparison table is built from."""
    headers, _ = auth(Role.REVIEWER)
    job = uploaded_job(headers)

    _run(client, headers, job["id"], "no_agent")
    _run(client, headers, job["id"], "agent")

    comparison = client.get(f"/api/v1/jobs/{job['id']}/comparison", headers=headers).json()

    assert comparison["agent"]["runs"] == 1
    assert comparison["no_agent_control"]["runs"] == 1
    assert comparison["agent"]["suspended_at_gate"] == 1
    assert comparison["no_agent_control"]["suspended_at_gate"] == 0

    interpretation = comparison["interpretation"]
    assert interpretation["human_gates_enforced"] == 1
    assert interpretation["writes_without_human_review"] == 1
    assert comparison["agent"]["traced_steps"] > 0


def test_audit_volume_is_far_higher_under_governance(client, auth, uploaded_job):
    """Accountability has a measurable footprint: count the evidence each arm leaves."""
    headers, _ = auth(Role.REVIEWER)

    agent_job = uploaded_job(headers)
    _run(client, headers, agent_job["id"], "agent")
    agent_events = client.get(f"/api/v1/jobs/{agent_job['id']}/audit", headers=headers).json()

    control_job = uploaded_job(headers)
    _run(client, headers, control_job["id"], "no_agent")
    control_events = client.get(f"/api/v1/jobs/{control_job['id']}/audit", headers=headers).json()

    agent_actions = {e["action"] for e in agent_events}
    control_actions = {e["action"] for e in control_events}

    # Only the governed arm records per-tool calls and a gate.
    assert "tool.call" in agent_actions
    assert "tool.call" not in control_actions
    assert "gate.opened" in agent_actions
    assert "gate.opened" not in control_actions
    assert len(agent_events) > len(control_events)
