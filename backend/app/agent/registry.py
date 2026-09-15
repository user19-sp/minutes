"""Tool allow-list registry -- the boundary the orchestrator cannot reach past.

The blueprint is explicit: *do not give the agent free-form tool access*. So the
orchestrator has no generic "run code", "query database" or "call URL" capability.
It can only name a tool from this registry, and every invocation passes five
checks before any code runs:

  1. the name exists in the registry            -> else DENIED (not_allowlisted)
  2. the name is in this run's allow-list       -> else DENIED (not_in_run_allowlist)
  3. neither the per-tool nor the per-run quota is exhausted  -> else DENIED (quota)
  4. the arguments validate against the tool's declared schema -> else DENIED (invalid_arguments)
  5. if the tool has a side effect, an approved gate exists    -> else APPROVAL_REQUIRED

Denials are not silent: each one is audited, counted in `agent_tool_calls_total`
and surfaced on the run. "The agent tried to do X and was stopped" is evidence,
and the security tests assert on it.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from backend.app.agent.audit import AuditLogger
from backend.app.observability.logging import get_logger
from backend.app.observability.metrics import agent_tool_calls_total, agent_tool_duration_seconds

log = get_logger("agent.registry")

SideEffect = Literal["read", "write", "external"]


class ToolDenied(Exception):
    """The allow-list refused the call. The run continues; the denial is recorded."""

    def __init__(self, tool: str, reason: str, detail: str = "") -> None:
        self.tool = tool
        self.reason = reason
        self.detail = detail
        super().__init__(f"tool {tool!r} denied: {reason}{f' ({detail})' if detail else ''}")


class ApprovalRequired(Exception):
    """A side-effecting tool was reached with no approved gate. The run suspends."""

    def __init__(self, tool: str, summary: str, payload: dict[str, Any], risk: str) -> None:
        self.tool = tool
        self.summary = summary
        self.payload = payload
        self.risk = risk
        super().__init__(f"tool {tool!r} requires human approval")


class ToolExecutionError(Exception):
    """The tool itself failed. Distinct from a denial -- governance worked, the tool did not."""


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    side_effect: SideEffect
    input_model: type[BaseModel]
    handler: Callable[..., Any]
    #: Side-effecting tools are gated by default. A `read` tool may still be gated
    #: (e.g. when it touches another user's data).
    requires_approval: bool = False
    max_calls_per_run: int = 5
    risk: str = "medium"

    def public(self) -> dict[str, Any]:
        """Shape rendered on the governance page so a reviewer can see the agent's
        exact capability surface."""
        return {
            "name": self.name,
            "description": self.description,
            "side_effect": self.side_effect,
            "requires_approval": self.requires_approval,
            "max_calls_per_run": self.max_calls_per_run,
            "risk": self.risk,
            "input_schema": self.input_model.model_json_schema(),
        }


@dataclass
class ToolContext:
    """Per-run state carried through every invocation.

    `allowed_tools` narrows the registry further for this particular run: a
    no-agent baseline run, for instance, gets the extraction tools but no export
    tool at all.
    """

    run_id: str
    job_id: str
    db: Any
    audit: AuditLogger
    allowed_tools: set[str]
    max_total_calls: int
    #: Gate keys ("tool:fingerprint") a human has already approved for this run.
    granted_approvals: set[str] = field(default_factory=set)
    call_counts: dict[str, int] = field(default_factory=dict)
    total_calls: int = 0
    denied_calls: int = 0
    trace: list[dict[str, Any]] = field(default_factory=list)

    def remaining(self) -> int:
        return max(0, self.max_total_calls - self.total_calls)


def approval_key(tool: str, payload: dict[str, Any]) -> str:
    """Bind an approval to the exact action approved.

    A human approving "export 4 approved actions as CSV" must not silently
    authorise "export 900 unapproved actions as JSON", so the key fingerprints the
    arguments rather than just the tool name.
    """
    import hashlib
    import json

    canonical = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return f"{tool}:{digest}"


class ToolRegistry:
    """The allow-list. Tools must be registered at import time, never at run time."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> ToolSpec:
        if spec.name in self._tools:
            raise ValueError(f"tool {spec.name!r} is already registered")
        if spec.side_effect in ("write", "external") and not spec.requires_approval:
            # Structural guard: a side-effecting tool that skips the gate is a bug,
            # not a configuration choice.
            raise ValueError(
                f"tool {spec.name!r} has side_effect={spec.side_effect!r} and must set "
                "requires_approval=True"
            )
        self._tools[spec.name] = spec
        return spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def names(self) -> set[str]:
        return set(self._tools)

    def specs(self) -> list[ToolSpec]:
        return sorted(self._tools.values(), key=lambda s: s.name)

    def read_only_names(self) -> set[str]:
        return {s.name for s in self._tools.values() if s.side_effect == "read"}

    # ---------------------------------------------------------------- invoke #

    def invoke(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> Any:
        """Run the five checks, then execute. Every path is audited."""
        started = time.perf_counter()

        spec = self._tools.get(name)
        if spec is None:
            self._deny(
                ctx, name, "not_allowlisted", f"no such tool; registry={sorted(self._tools)}"
            )

        if name not in ctx.allowed_tools:
            self._deny(
                ctx, name, "not_in_run_allowlist", f"run permits {sorted(ctx.allowed_tools)}"
            )

        if ctx.total_calls >= ctx.max_total_calls:
            self._deny(ctx, name, "run_quota_exhausted", f"limit={ctx.max_total_calls}")

        used = ctx.call_counts.get(name, 0)
        if used >= spec.max_calls_per_run:
            self._deny(ctx, name, "tool_quota_exhausted", f"limit={spec.max_calls_per_run}")

        try:
            validated = spec.input_model.model_validate(arguments)
        except ValidationError as exc:
            detail = str(exc.errors(include_url=False))[:400]
            self._deny(ctx, name, "invalid_arguments", detail)
            return None  # unreachable; _deny raises

        if spec.requires_approval:
            key = approval_key(name, validated.model_dump(mode="json"))
            if key not in ctx.granted_approvals:
                ctx.audit.agent(
                    "tool.approval_required",
                    run_id=ctx.run_id,
                    job_id=ctx.job_id,
                    outcome="suspended",
                    resource_type="tool",
                    resource_id=name,
                    detail={"arguments": validated.model_dump(mode="json"), "gate_key": key},
                )
                raise ApprovalRequired(
                    tool=name,
                    summary=spec.description,
                    payload=validated.model_dump(mode="json"),
                    risk=spec.risk,
                )

        # ---- all checks passed ------------------------------------------- #
        ctx.call_counts[name] = used + 1
        ctx.total_calls += 1

        try:
            result = spec.handler(validated, ctx)
        except (ToolDenied, ApprovalRequired):
            # A handler may refuse mid-execution (e.g. a job-scope violation). That
            # is a governance outcome, already audited by `deny_tool_call`, not a
            # tool failure -- re-raise it unchanged so the orchestrator classifies
            # it correctly and it is not audited a second time as an error.
            ctx.call_counts[name] = used
            ctx.total_calls -= 1
            raise
        except Exception as exc:
            elapsed_ms = (time.perf_counter() - started) * 1000
            agent_tool_calls_total.labels(tool=name, outcome="error").inc()
            ctx.audit.agent(
                "tool.error",
                run_id=ctx.run_id,
                job_id=ctx.job_id,
                outcome="error",
                resource_type="tool",
                resource_id=name,
                duration_ms=elapsed_ms,
                detail={"error_type": type(exc).__name__, "error": str(exc)[:500]},
            )
            ctx.trace.append(
                {
                    "tool": name,
                    "outcome": "error",
                    "error": str(exc)[:200],
                    "duration_ms": round(elapsed_ms, 2),
                }
            )
            raise ToolExecutionError(f"{name} failed: {exc}") from exc

        elapsed_ms = (time.perf_counter() - started) * 1000
        agent_tool_calls_total.labels(tool=name, outcome="allowed").inc()
        agent_tool_duration_seconds.labels(tool=name).observe(elapsed_ms / 1000)
        ctx.audit.agent(
            "tool.call",
            run_id=ctx.run_id,
            job_id=ctx.job_id,
            resource_type="tool",
            resource_id=name,
            duration_ms=elapsed_ms,
            detail={
                "arguments": validated.model_dump(mode="json"),
                "side_effect": spec.side_effect,
                "result_summary": _summarise(result),
                "call_number": ctx.total_calls,
            },
        )
        ctx.trace.append(
            {
                "tool": name,
                "outcome": "allowed",
                "duration_ms": round(elapsed_ms, 2),
                "result_summary": _summarise(result),
            }
        )
        return result

    # ------------------------------------------------------------- internals #

    @staticmethod
    def _deny(ctx: ToolContext, name: str, reason: str, detail: str = "") -> None:
        deny_tool_call(ctx, name, reason, detail)


def deny_tool_call(ctx: ToolContext, name: str, reason: str, detail: str = "") -> None:
    """Record a refusal and abort the call.

    Exposed at module level so a tool handler can refuse mid-execution (for
    example, on a job-scope violation) through exactly the same audited path the
    registry uses -- there is no way to refuse a call without leaving evidence.
    """
    ctx.denied_calls += 1
    agent_tool_calls_total.labels(tool=name, outcome=f"denied_{reason}").inc()
    ctx.audit.agent(
        "tool.denied",
        run_id=ctx.run_id,
        job_id=ctx.job_id,
        outcome="denied",
        resource_type="tool",
        resource_id=name,
        detail={"reason": reason, "detail": detail},
    )
    ctx.trace.append({"tool": name, "outcome": "denied", "reason": reason})
    log.warning("tool_denied", tool=name, reason=reason, run_id=ctx.run_id)
    raise ToolDenied(name, reason, detail)


def _summarise(result: Any) -> Any:
    """Keep audit payloads small: record shape, not content."""
    if result is None:
        return None
    if isinstance(result, dict):
        return {k: _summarise(v) for k, v in list(result.items())[:20]}
    if isinstance(result, list | tuple):
        return {"type": "list", "length": len(result)}
    if isinstance(result, str):
        return {"type": "str", "length": len(result)}
    if isinstance(result, int | float | bool):
        return result
    return {"type": type(result).__name__}


#: The process-wide allow-list. Populated by `backend.app.agent.tools` at import.
registry = ToolRegistry()
