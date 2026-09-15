"""Excessive-tool-permission tests (threat T-02).

The claim under test: the orchestrator's capability surface is exactly the
allow-list, and nothing widens it at run time. Each test tries a different way to
get more capability than granted, and asserts the refusal is audited.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from backend.app.agent.audit import AuditLogger
from backend.app.agent.registry import (
    ToolContext,
    ToolDenied,
    ToolRegistry,
    ToolSpec,
    approval_key,
    registry,
)
from backend.app.models import ActorType, AuditEvent, Role


class _Args(BaseModel):
    job_id: str


def _ctx(db, allowed: set[str] | None = None, max_calls: int = 25) -> ToolContext:
    return ToolContext(
        run_id="00000000-0000-0000-0000-000000000001",
        job_id="00000000-0000-0000-0000-0000000000aa",
        db=db,
        audit=AuditLogger(db),
        allowed_tools=allowed if allowed is not None else set(registry.names()),
        max_total_calls=max_calls,
    )


# --------------------------------------------------------------------------- #
# Registry-level guarantees
# --------------------------------------------------------------------------- #


def test_every_side_effecting_tool_is_gated():
    """Structural invariant: no write or external tool may exist without a gate."""
    for spec in registry.specs():
        if spec.side_effect in ("write", "external"):
            assert spec.requires_approval, f"{spec.name} has a side effect but no approval gate"


def test_registry_refuses_to_register_an_ungated_write_tool():
    """The guard is enforced at registration, so the mistake cannot ship."""
    local = ToolRegistry()
    with pytest.raises(ValueError, match="must set requires_approval"):
        local.register(
            ToolSpec(
                name="sneaky_write",
                description="writes without asking",
                side_effect="write",
                input_model=_Args,
                handler=lambda a, c: None,
                requires_approval=False,
            )
        )


def test_allow_list_is_the_documented_set():
    """If someone adds a tool, this test fails until the docs and report agree."""
    assert registry.names() == {
        "transcribe_audio",
        "diarize_speakers",
        "segment_agenda",
        "extract_decisions_actions",
        "persist_minutes",
        "export_actions",
    }


# --------------------------------------------------------------------------- #
# Invocation-level refusals
# --------------------------------------------------------------------------- #


def test_unregistered_tool_is_denied_and_audited(db):
    ctx = _ctx(db)
    with pytest.raises(ToolDenied) as exc:
        registry.invoke("run_shell_command", {"cmd": "rm -rf /"}, ctx)

    assert exc.value.reason == "not_allowlisted"
    assert ctx.denied_calls == 1
    assert ctx.total_calls == 0
    db.commit()

    events = db.query(AuditEvent).filter(AuditEvent.action == "tool.denied").all()
    assert len(events) == 1
    assert events[0].outcome == "denied"
    assert events[0].actor_type is ActorType.AGENT
    assert events[0].detail["reason"] == "not_allowlisted"


def test_tool_outside_this_runs_allow_list_is_denied(db):
    """A run narrowed to the pipeline tools cannot reach export."""
    ctx = _ctx(db, allowed={"transcribe_audio", "segment_agenda"})
    with pytest.raises(ToolDenied) as exc:
        registry.invoke("export_actions", {"job_id": ctx.job_id, "format": "csv"}, ctx)
    assert exc.value.reason == "not_in_run_allowlist"


def test_per_run_call_budget_stops_a_runaway_agent(db):
    ctx = _ctx(db, max_calls=0)
    with pytest.raises(ToolDenied) as exc:
        registry.invoke("transcribe_audio", {"job_id": ctx.job_id}, ctx)
    assert exc.value.reason == "run_quota_exhausted"


def test_per_tool_call_budget_is_enforced(db):
    ctx = _ctx(db)
    ctx.call_counts["persist_minutes"] = 1  # spec allows 1 per run
    with pytest.raises(ToolDenied) as exc:
        registry.invoke(
            "persist_minutes",
            {"job_id": ctx.job_id, "source_tool": "x", "decisions": [], "actions": []},
            ctx,
        )
    assert exc.value.reason == "tool_quota_exhausted"


def test_malformed_arguments_are_denied_before_the_handler_runs(db):
    ctx = _ctx(db)
    with pytest.raises(ToolDenied) as exc:
        registry.invoke("segment_agenda", {"job_id": ctx.job_id, "text": ""}, ctx)
    assert exc.value.reason == "invalid_arguments"
    assert ctx.total_calls == 0, "a rejected call must not count against the budget as executed"


def test_tool_cannot_operate_on_another_users_job(client, auth, uploaded_job, db):
    """Scope confinement: a tool call naming a different job is refused at the
    tool boundary even though the API already checked ownership."""
    headers_a, _ = auth(Role.REVIEWER)
    headers_b, _ = auth(Role.REVIEWER)
    job_a = uploaded_job(headers_a)
    job_b = uploaded_job(headers_b)

    ctx = _ctx(db)
    ctx.job_id = job_a["id"]

    with pytest.raises(ToolDenied) as exc:
        registry.invoke("transcribe_audio", {"job_id": job_b["id"]}, ctx)
    assert exc.value.reason == "job_scope_violation"


def test_approval_fingerprint_binds_to_exact_arguments():
    """Approving one action must not authorise a different one."""
    approved = {"job_id": "j1", "format": "csv", "include_unapproved": False}
    escalated = {"job_id": "j1", "format": "csv", "include_unapproved": True}
    other_job = {"job_id": "j2", "format": "csv", "include_unapproved": False}

    key = approval_key("export_actions", approved)
    assert key == approval_key("export_actions", dict(reversed(list(approved.items()))))
    assert key != approval_key("export_actions", escalated)
    assert key != approval_key("export_actions", other_job)
    assert key != approval_key("persist_minutes", approved)


def test_gated_tool_without_a_grant_raises_approval_not_execution(db):
    """The gate fires before the handler, so no side effect occurs while waiting."""
    from backend.app.agent.registry import ApprovalRequired

    ctx = _ctx(db)
    with pytest.raises(ApprovalRequired) as exc:
        registry.invoke(
            "persist_minutes",
            {"job_id": ctx.job_id, "source_tool": "t", "decisions": [], "actions": []},
            ctx,
        )
    assert exc.value.tool == "persist_minutes"
    assert ctx.total_calls == 0, "a suspended call has not executed"


def test_forged_grant_for_different_arguments_does_not_unlock_the_tool(db):
    """Holding a grant for payload A must not let payload B through."""
    from backend.app.agent.registry import ApprovalRequired

    ctx = _ctx(db)
    ctx.granted_approvals = {
        approval_key(
            "persist_minutes",
            {"job_id": ctx.job_id, "source_tool": "t", "decisions": [], "actions": []},
        )
    }

    # Same tool, different payload: one extra action item smuggled in.
    with pytest.raises(ApprovalRequired):
        registry.invoke(
            "persist_minutes",
            {
                "job_id": ctx.job_id,
                "source_tool": "t",
                "decisions": [],
                "actions": [
                    {
                        "text": "smuggled task",
                        "owner_name": None,
                        "deadline": None,
                        "confidence": 0.9,
                        "evidence_quote": None,
                        "agenda_position": None,
                    }
                ],
            },
            ctx,
        )


def test_governance_surface_is_published_to_reviewers(client, auth):
    """A reviewer can read the agent's capability surface from the running system."""
    headers, _ = auth(Role.REVIEWER)

    tools = client.get("/api/v1/governance/tools", headers=headers).json()
    assert {t["name"] for t in tools} == registry.names()
    gated = {t["name"] for t in tools if t["requires_approval"]}
    assert gated == {"persist_minutes", "export_actions"}
    assert all(t["input_schema"] for t in tools)

    policy = client.get("/api/v1/governance/policy", headers=headers).json()
    assert policy["gated_tools"] == ["export_actions", "persist_minutes"]
    assert policy["auto_approve_enabled"] is False, "nothing may be auto-approved by default"
