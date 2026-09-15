"""Prometheus metrics exposed at /metrics.

Split into three families:
  * http_*       -- standard request/latency/error signals
  * agent_*      -- governance signals (tool calls, denials, gate outcomes)
  * reviewer_*   -- human-in-the-loop signals (correction time, edit rate)

The agent_* and reviewer_* families are the ones the evaluation dossier reads;
they are the quantitative half of the agent-vs-no-agent comparison.
"""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, multiprocess
from prometheus_client.core import REGISTRY as DEFAULT_REGISTRY

registry: CollectorRegistry = DEFAULT_REGISTRY

# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

http_requests_total = Counter(
    "http_requests_total",
    "HTTP requests processed.",
    ["method", "path", "status"],
)

http_request_duration_seconds = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency.",
    ["method", "path"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

# --------------------------------------------------------------------------- #
# Pipeline
# --------------------------------------------------------------------------- #

jobs_total = Counter("jobs_total", "Jobs created.", ["status"])

pipeline_stage_duration_seconds = Histogram(
    "pipeline_stage_duration_seconds",
    "Duration of one pipeline stage.",
    ["stage"],
    buckets=(0.05, 0.1, 0.5, 1.0, 5.0, 15.0, 60.0, 300.0),
)

# --------------------------------------------------------------------------- #
# Agent governance
# --------------------------------------------------------------------------- #

agent_runs_total = Counter("agent_runs_total", "Orchestrator runs.", ["mode", "status"])

agent_run_duration_seconds = Histogram(
    "agent_run_duration_seconds",
    "End-to-end orchestrator run duration.",
    ["mode"],
    buckets=(0.1, 0.5, 1.0, 5.0, 15.0, 60.0, 300.0),
)

agent_tool_calls_total = Counter(
    "agent_tool_calls_total",
    "Tool invocations attempted by the orchestrator.",
    ["tool", "outcome"],  # outcome: allowed | denied_not_allowlisted | denied_quota | error
)

agent_tool_duration_seconds = Histogram(
    "agent_tool_duration_seconds",
    "Tool execution time.",
    ["tool"],
    buckets=(0.01, 0.05, 0.1, 0.5, 1.0, 5.0, 30.0),
)

approval_gates_total = Counter(
    "approval_gates_total",
    "Approval gates opened and their eventual outcome.",
    ["action", "status"],  # status: requested | approved | rejected
)

approval_wait_seconds = Histogram(
    "approval_wait_seconds",
    "Time a gate spent pending a human decision.",
    ["action"],
    buckets=(5, 30, 60, 300, 900, 3600, 86400),
)

pending_approvals = Gauge("pending_approvals", "Approval gates currently awaiting a human.")

# --------------------------------------------------------------------------- #
# Job queue
# --------------------------------------------------------------------------- #

queue_depth = Gauge("queue_depth", "Runs waiting to be picked up by a worker.")

queue_runs_total = Counter(
    "queue_runs_total",
    "Queued run lifecycle events.",
    ["mode", "outcome"],  # enqueued | completed | failed | requeued | abandoned
)

queue_claim_latency_seconds = Histogram(
    "queue_claim_latency_seconds",
    "Time a worker spent claiming a run from the queue.",
    buckets=(0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0),
)

queue_wait_seconds = Histogram(
    "queue_wait_seconds",
    "Time a run waited in the queue before a worker started it.",
    ["mode"],
    buckets=(0.5, 1, 5, 15, 60, 300, 900),
)

injection_attempts_total = Counter(
    "injection_attempts_total",
    "Prompt-injection patterns detected in untrusted content.",
    ["rule", "severity"],
)

pii_redactions_total = Counter(
    "pii_redactions_total",
    "Identifiers redacted before storage.",
    ["kind"],
)

# --------------------------------------------------------------------------- #
# Reviewer (human-in-the-loop)
# --------------------------------------------------------------------------- #

reviewer_decisions_total = Counter(
    "reviewer_decisions_total",
    "Reviewer rulings on extracted items.",
    ["item_type", "status"],  # status: approved | rejected | edited
)

reviewer_correction_seconds = Histogram(
    "reviewer_correction_seconds",
    "Wall-clock seconds a reviewer spent on one item.",
    ["item_type"],
    buckets=(1, 2, 5, 10, 20, 30, 60, 120, 300),
)

exports_total = Counter("exports_total", "Export files produced.", ["format"])


def enable_multiprocess(path: str) -> CollectorRegistry:
    """For gunicorn/uvicorn multi-worker deployments (PROMETHEUS_MULTIPROC_DIR)."""
    reg = CollectorRegistry()
    multiprocess.MultiProcessCollector(reg, path=path)
    return reg
