"""Latency and load measurement for the API.

    python scripts/loadtest.py --users 10 --requests 20
    python scripts/loadtest.py --scenario pipeline --users 4 --requests 5

Covers the blueprint's "locust or simple load script (latency)" requirement
without adding a dependency: the standard library and httpx (already a test
dependency) are enough for the scale this prototype needs, and a self-contained
script is easier for an examiner to run than a locust deployment.

What it measures
----------------
Per-endpoint p50/p95/p99 and throughput, plus governance-specific figures the
generic tools would not know to collect:

  * **gate latency** -- how long a governed run takes to reach its approval gate.
    This is the number a reviewer actually waits for.
  * **governance overhead** -- the same pipeline run with and without the agent
    layer. The difference is the measured price of the allow-list, the gate and
    the audit trail, and it belongs in the comparison study rather than being
    hand-waved as "negligible".

Scenarios
---------
  read      auth + read-only endpoints. Establishes baseline API latency.
  pipeline  full governed run per user: upload, run to gate, approve, review.
            The realistic mixed workload.
  overhead  agent vs no-agent, same input, for the comparison table.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import statistics
import struct
import sys
import threading
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

try:
    import httpx
except ImportError:  # pragma: no cover
    sys.exit("httpx is required: pip install -r requirements-dev.txt")

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASE = "http://127.0.0.1:8000"

SAMPLE_TRANSCRIPT = """Good morning, let us begin the sprint review.
Priya will send the updated API contract to the vendor by Friday.
We have decided to postpone the localisation release until next quarter.
Moving on to the infrastructure migration.
Rahul needs to check the staging backups before the cutover.
It was agreed that we will go with the managed Postgres option.
"""


# --------------------------------------------------------------------------- #
# Measurement
# --------------------------------------------------------------------------- #


@dataclass
class Samples:
    """Latency samples for one labelled operation."""

    label: str
    durations_ms: list[float] = field(default_factory=list)
    errors: int = 0

    def add(self, ms: float) -> None:
        self.durations_ms.append(ms)

    def percentile(self, p: float) -> float:
        if not self.durations_ms:
            return 0.0
        ordered = sorted(self.durations_ms)
        # Nearest-rank: with the small samples a prototype produces, interpolating
        # between two points invents precision that is not there.
        rank = round(p / 100 * len(ordered) + 0.5)
        index = min(len(ordered) - 1, max(0, rank - 1))
        return ordered[index]

    def summary(self) -> dict:
        if not self.durations_ms:
            return {"label": self.label, "n": 0, "errors": self.errors}
        return {
            "label": self.label,
            "n": len(self.durations_ms),
            "errors": self.errors,
            "min_ms": round(min(self.durations_ms), 1),
            "p50_ms": round(self.percentile(50), 1),
            "p95_ms": round(self.percentile(95), 1),
            "p99_ms": round(self.percentile(99), 1),
            "max_ms": round(max(self.durations_ms), 1),
            "mean_ms": round(statistics.fmean(self.durations_ms), 1),
        }


class Collector:
    def __init__(self) -> None:
        self._samples: dict[str, Samples] = defaultdict(lambda: Samples(""))
        self._lock = threading.Lock()

    def record(self, label: str, ms: float, ok: bool = True) -> None:
        with self._lock:
            bucket = self._samples.setdefault(label, Samples(label))
            bucket.label = label
            if ok:
                bucket.add(ms)
            else:
                bucket.errors += 1

    def report(self) -> list[dict]:
        with self._lock:
            return [s.summary() for s in self._samples.values() if s.durations_ms or s.errors]


def timed(collector: Collector, label: str):
    """Context manager recording wall-clock latency for one operation."""

    class _Timer:
        def __enter__(self):
            self.start = time.perf_counter()
            return self

        def __exit__(self, exc_type, exc, tb):
            elapsed = (time.perf_counter() - self.start) * 1000
            collector.record(label, elapsed, ok=exc_type is None)
            return False

    return _Timer()


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


def wav_bytes(seconds: float = 0.5, rate: int = 8000) -> bytes:
    data = b"\x00\x00" * int(seconds * rate)
    header = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE"
    header += b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
    header += b"data" + struct.pack("<I", len(data))
    return header + data


ADMIN_EMAIL = os.getenv("MOM_ADMIN_EMAIL", "admin@example.com")
ADMIN_PASSWORD = os.getenv("MOM_ADMIN_PASSWORD", "admin-demo-password")


def _admin_token(base: str) -> str | None:
    """Sign in as admin so generated load-test users can be approved.

    Registration is approved-only by default, so this script has to clear each
    address it invents. Returns None when there is no admin account, and the
    caller then just registers -- which works if registration mode is "open".
    """
    with httpx.Client(base_url=base, timeout=30) as client:
        resp = client.post(
            "/api/v1/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}
        )
        return resp.json()["access_token"] if resp.status_code == 200 else None


def ensure_user(base: str, email: str, password: str, admin_token: str | None = None) -> str:
    """Approve, register (ignoring conflict) and return a bearer token."""
    with httpx.Client(base_url=base, timeout=30) as client:
        if admin_token:
            client.post(
                "/api/v1/auth/approved-emails",
                headers={"Authorization": f"Bearer {admin_token}"},
                json={"email": email, "note": "load test"},
            )
        client.post(
            "/api/v1/auth/register",
            json={"email": email, "password": password, "role": "reviewer"},
        )
        resp = client.post("/api/v1/auth/login", json={"email": email, "password": password})
        if resp.status_code == 429:
            sys.exit(
                "Rate limited while setting up. The limiter is doing its job -- wait "
                "15 minutes, or restart the API to clear the in-process counters."
            )
        resp.raise_for_status()
        return resp.json()["access_token"]


# --------------------------------------------------------------------------- #
# Scenarios
# --------------------------------------------------------------------------- #


def scenario_read(base: str, token: str, requests: int, collector: Collector) -> None:
    headers = {"Authorization": f"Bearer {token}"}
    endpoints = [
        ("GET /health", "/health", False),
        ("GET /metrics", "/metrics", False),
        ("GET /jobs", "/api/v1/jobs", True),
        ("GET /governance/tools", "/api/v1/governance/tools", True),
        ("GET /governance/policy", "/api/v1/governance/policy", True),
        ("GET /approvals", "/api/v1/approvals", True),
    ]
    with httpx.Client(base_url=base, timeout=30) as client:
        for _ in range(requests):
            for label, path, authed in endpoints:
                with timed(collector, label):
                    resp = client.get(path, headers=headers if authed else {})
                    resp.raise_for_status()


def scenario_pipeline(base: str, token: str, requests: int, collector: Collector) -> None:
    """Upload -> run to gate -> approve -> review. The realistic workload."""
    headers = {"Authorization": f"Bearer {token}"}
    with httpx.Client(base_url=base, timeout=120) as client:
        for _ in range(requests):
            with timed(collector, "POST /jobs (upload)"):
                created = client.post(
                    "/api/v1/jobs",
                    headers=headers,
                    files={
                        "file": (
                            f"load-{uuid.uuid4().hex[:8]}.txt",
                            io.BytesIO(SAMPLE_TRANSCRIPT.encode()),
                            "text/plain",
                        )
                    },
                    data={"title": "load test"},
                )
                created.raise_for_status()
            job_id = created.json()["id"]

            # The number a reviewer actually waits for.
            with timed(collector, "POST /runs (upload -> gate)"):
                run = client.post(
                    f"/api/v1/jobs/{job_id}/runs", headers=headers, json={"mode": "agent"}
                )
                run.raise_for_status()

            with timed(collector, "GET /minutes"):
                minutes = client.get(f"/api/v1/jobs/{job_id}/minutes", headers=headers)
                minutes.raise_for_status()

            gates = minutes.json()["pending_approvals"]
            if not gates:
                continue

            with timed(collector, "POST /approvals/decide (gate -> written)"):
                decided = client.post(
                    f"/api/v1/approvals/{gates[0]['id']}/decide",
                    headers=headers,
                    json={"decision": "approved"},
                )
                decided.raise_for_status()

            items = client.get(f"/api/v1/jobs/{job_id}/minutes", headers=headers).json()
            for action in items["action_items"][:3]:
                with timed(collector, "POST /actions/review"):
                    client.post(
                        f"/api/v1/actions/{action['id']}/review",
                        headers=headers,
                        json={"status": "approved", "review_seconds": 3.0},
                    ).raise_for_status()


def scenario_overhead(base: str, token: str, requests: int, collector: Collector) -> None:
    """Agent vs no-agent on identical input: the price of governance."""
    headers = {"Authorization": f"Bearer {token}"}
    with httpx.Client(base_url=base, timeout=120) as client:
        for mode in ("agent", "no_agent"):
            for _ in range(requests):
                created = client.post(
                    "/api/v1/jobs",
                    headers=headers,
                    files={
                        "file": (
                            f"ovh-{uuid.uuid4().hex[:8]}.txt",
                            io.BytesIO(SAMPLE_TRANSCRIPT.encode()),
                            "text/plain",
                        )
                    },
                    data={"title": f"overhead {mode}"},
                )
                created.raise_for_status()
                job_id = created.json()["id"]

                with timed(collector, f"run [{mode}]"):
                    client.post(
                        f"/api/v1/jobs/{job_id}/runs", headers=headers, json={"mode": mode}
                    ).raise_for_status()


SCENARIOS = {
    "read": scenario_read,
    "pipeline": scenario_pipeline,
    "overhead": scenario_overhead,
}


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #


def run(base: str, scenario: str, users: int, requests: int) -> dict:
    collector = Collector()
    fn = SCENARIOS[scenario]

    print(f"  target    : {base}")
    print(f"  scenario  : {scenario}")
    print(f"  users     : {users} concurrent")
    print(f"  requests  : {requests} iterations each\n")

    admin_token = _admin_token(base)
    tokens = []
    for _ in range(users):
        email = f"loadtest-{uuid.uuid4().hex[:10]}@example.com"
        tokens.append(ensure_user(base, email, "load-test-password-123", admin_token))

    errors: list[str] = []

    def worker(token: str) -> None:
        try:
            fn(base, token, requests, collector)
        except Exception as exc:  # one worker failing must not stop the whole run
            errors.append(f"{type(exc).__name__}: {exc}")

    started = time.perf_counter()
    threads = [threading.Thread(target=worker, args=(t,), daemon=True) for t in tokens]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = time.perf_counter() - started

    rows = collector.report()
    rows.sort(key=lambda r: r.get("p95_ms", 0), reverse=True)

    total = sum(r.get("n", 0) for r in rows)
    total_errors = sum(r.get("errors", 0) for r in rows)

    print(f"  {'operation':38} {'n':>5} {'p50':>8} {'p95':>8} {'p99':>8} {'max':>8}")
    print(f"  {'-' * 38} {'-' * 5} {'-' * 8} {'-' * 8} {'-' * 8} {'-' * 8}")
    for r in rows:
        if not r.get("n"):
            print(f"  {r['label']:38} {'-':>5} {'all failed':>35}")
            continue
        print(
            f"  {r['label']:38} {r['n']:>5} {r['p50_ms']:>7.1f}m {r['p95_ms']:>7.1f}m "
            f"{r['p99_ms']:>7.1f}m {r['max_ms']:>7.1f}m"
        )

    print(f"\n  wall clock    : {wall:.2f}s")
    print(f"  completed     : {total} operations")
    print(f"  throughput    : {total / wall:.1f} ops/s")
    print(f"  errors        : {total_errors}")

    summary = {
        "base_url": base,
        "scenario": scenario,
        "users": users,
        "requests_per_user": requests,
        "wall_seconds": round(wall, 2),
        "throughput_ops_per_sec": round(total / wall, 2) if wall else 0,
        "total_operations": total,
        "total_errors": total_errors,
        "operations": rows,
    }

    if scenario == "overhead":
        agent = next((r for r in rows if r["label"] == "run [agent]"), None)
        control = next((r for r in rows if r["label"] == "run [no_agent]"), None)
        if agent and control and agent.get("n") and control.get("n"):
            delta = agent["p50_ms"] - control["p50_ms"]
            pct = (delta / control["p50_ms"] * 100) if control["p50_ms"] else 0
            print("\n  Governance overhead (p50)")
            print(f"    governed   : {agent['p50_ms']:.1f} ms")
            print(f"    ungoverned : {control['p50_ms']:.1f} ms")
            print(f"    difference : {delta:+.1f} ms ({pct:+.1f}%)")
            print(
                "\n    That difference is the measured price of the allow-list, the\n"
                "    approval gate and the audit trail."
            )
            summary["governance_overhead_ms"] = round(delta, 1)
            summary["governance_overhead_pct"] = round(pct, 1)

    if errors:
        print(f"\n  worker failures ({len(errors)}):")
        for e in errors[:5]:
            print(f"    {e}")

    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--base", default=DEFAULT_BASE, help=f"API base URL (default {DEFAULT_BASE})"
    )
    parser.add_argument(
        "--scenario", default="read", choices=sorted(SCENARIOS), help="workload to run"
    )
    parser.add_argument("--users", type=int, default=5, help="concurrent users")
    parser.add_argument("--requests", type=int, default=10, help="iterations per user")
    parser.add_argument("--json", type=Path, help="write the summary to this path")
    args = parser.parse_args()

    print("\n  Meeting Intelligence - load and latency test\n")

    try:
        httpx.get(f"{args.base}/health", timeout=5).raise_for_status()
    except Exception as exc:
        return int(bool(sys.stderr.write(f"  API not reachable at {args.base}: {exc}\n")) or 1)

    summary = run(args.base, args.scenario, args.users, args.requests)

    if args.json:
        args.json.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(f"\n  summary written to {args.json}")

    print()
    return 1 if summary["total_errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
