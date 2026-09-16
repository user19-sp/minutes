"""Brute-force protection (threat model R1).

R1 was the highest-severity open risk: password guessing against /auth/login was
unthrottled. These tests pin the behaviour that closes it.
"""

from __future__ import annotations

import pytest

from backend.app.models import Role
from backend.app.security.ratelimit import (
    RateLimiter,
    RateLimitExceeded,
    login_account_limiter,
    login_ip_limiter,
    register_ip_limiter,
)

# --------------------------------------------------------------------------- #
# The limiter itself
# --------------------------------------------------------------------------- #


def test_budget_is_spent_then_blocked():
    limiter = RateLimiter(max_attempts=3, window_seconds=60, block_seconds=30, name="t")

    for expected_remaining in (2, 1, 0):
        assert limiter.hit("k").remaining == expected_remaining

    with pytest.raises(RateLimitExceeded) as exc:
        limiter.hit("k")
    assert exc.value.retry_after == 30
    assert exc.value.scope == "t"


def test_keys_are_independent():
    """One attacker must not lock out everyone else."""
    limiter = RateLimiter(max_attempts=2, window_seconds=60, block_seconds=30, name="t")
    limiter.hit("attacker")
    limiter.hit("attacker")
    with pytest.raises(RateLimitExceeded):
        limiter.hit("attacker")

    assert limiter.hit("innocent").allowed


def test_reset_clears_the_budget():
    limiter = RateLimiter(max_attempts=2, window_seconds=60, block_seconds=30, name="t")
    limiter.hit("k")
    limiter.hit("k")
    limiter.reset("k")
    assert limiter.hit("k").remaining == 1


def test_window_slides(monkeypatch):
    """Attempts older than the window stop counting."""
    limiter = RateLimiter(max_attempts=2, window_seconds=10, block_seconds=30, name="t")

    clock = {"now": 1000.0}
    monkeypatch.setattr("backend.app.security.ratelimit.time.monotonic", lambda: clock["now"])

    limiter.hit("k")
    limiter.hit("k")

    clock["now"] += 11  # both attempts now fall outside the window
    assert limiter.hit("k").allowed, "expired attempts should not count"


def test_block_persists_for_its_full_cooldown(monkeypatch):
    """The cooldown governs once tripped -- it must not lift early."""
    limiter = RateLimiter(max_attempts=1, window_seconds=10, block_seconds=60, name="t")

    clock = {"now": 1000.0}
    monkeypatch.setattr("backend.app.security.ratelimit.time.monotonic", lambda: clock["now"])

    limiter.hit("k")
    with pytest.raises(RateLimitExceeded):
        limiter.hit("k")

    # Past the sliding window, but still inside the block.
    clock["now"] += 30
    with pytest.raises(RateLimitExceeded):
        limiter.hit("k")

    clock["now"] += 31  # cooldown over
    assert limiter.hit("k").allowed


def test_check_does_not_consume_budget():
    limiter = RateLimiter(max_attempts=2, window_seconds=60, block_seconds=30, name="t")
    for _ in range(10):
        assert limiter.check("k").remaining == 2
    assert limiter.hit("k").remaining == 1


def test_idle_buckets_are_swept(monkeypatch):
    """Unbounded growth per distinct IP would itself be a memory-exhaustion vector."""
    limiter = RateLimiter(max_attempts=5, window_seconds=10, block_seconds=1, name="t")

    clock = {"now": 1000.0}
    monkeypatch.setattr("backend.app.security.ratelimit.time.monotonic", lambda: clock["now"])

    for i in range(50):
        limiter.hit(f"ip-{i}")
    assert len(limiter._buckets) == 50

    clock["now"] += 120  # everything expires, and the sweep interval passes
    limiter.hit("fresh")
    assert len(limiter._buckets) == 1, "stale buckets should have been evicted"


# --------------------------------------------------------------------------- #
# Enforcement on the endpoints
# --------------------------------------------------------------------------- #


def test_password_guessing_is_blocked_with_429(client, reviewer_factory):
    """The core R1 case: repeated wrong passwords against one account."""
    _, email, _password = reviewer_factory(Role.REVIEWER)

    statuses = []
    for _ in range(8):
        resp = client.post("/api/v1/auth/login", json={"email": email, "password": "wrong-pw-here"})
        statuses.append(resp.status_code)

    assert 429 in statuses, "guessing was never throttled"
    # The account limiter (5) trips before the IP limiter (10).
    assert statuses.count(401) <= 5

    blocked = client.post("/api/v1/auth/login", json={"email": email, "password": "wrong-pw-here"})
    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) > 0
    assert "Too many attempts" in blocked.json()["detail"]


def test_lockout_holds_even_with_the_correct_password(client, reviewer_factory):
    """Once blocked, the attacker must not get in by finally guessing right."""
    _, email, password = reviewer_factory(Role.REVIEWER)

    for _ in range(6):
        client.post("/api/v1/auth/login", json={"email": email, "password": "wrong-pw-here"})

    resp = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 429, "a blocked account must stay blocked"


def test_a_successful_login_clears_the_account_budget(client, reviewer_factory):
    """An honest user who mistypes twice should not be one attempt from lockout."""
    _, email, password = reviewer_factory(Role.REVIEWER)

    for _ in range(2):
        client.post("/api/v1/auth/login", json={"email": email, "password": "wrong-pw-here"})
    assert (
        client.post("/api/v1/auth/login", json={"email": email, "password": password}).status_code
        == 200
    )

    # Budget restored: two more mistypes still leave room.
    for _ in range(2):
        assert (
            client.post(
                "/api/v1/auth/login", json={"email": email, "password": "wrong-pw-here"}
            ).status_code
            == 401
        )


def test_one_attacked_account_does_not_lock_out_others(client, reviewer_factory):
    """Per-account budgets must be independent, or an attacker could deny service
    to every user by attacking one."""
    _, victim_email, _ = reviewer_factory(Role.REVIEWER)
    _, other_email, other_password = reviewer_factory(Role.REVIEWER)

    for _ in range(6):
        client.post("/api/v1/auth/login", json={"email": victim_email, "password": "wrong-pw-here"})
    login_ip_limiter.clear()  # isolate the account limiter from the shared test IP

    resp = client.post(
        "/api/v1/auth/login", json={"email": other_email, "password": other_password}
    )
    assert resp.status_code == 200, "an unrelated account was collaterally locked out"


def test_spraying_many_accounts_from_one_host_is_blocked(client):
    """Per-account budgets alone would miss this: one attempt each against many
    emails never exhausts any single account."""
    statuses = [
        client.post(
            "/api/v1/auth/login",
            json={"email": f"victim{i}@example.com", "password": "wrong-pw-here"},
        ).status_code
        for i in range(25)
    ]
    assert 429 in statuses, "credential spraying from one IP was not throttled"


def test_registration_is_rate_limited(client):
    statuses = []
    for i in range(25):
        resp = client.post(
            "/api/v1/auth/register",
            json={"email": f"bulk{i}@example.com", "password": "a-long-enough-password"},
        )
        statuses.append(resp.status_code)
    assert 429 in statuses, "automated account creation was not throttled"


def test_rate_limit_refusals_are_audited(client, reviewer_factory, db):
    """A burst of these is how you notice you are being attacked."""
    from backend.app.models import AuditEvent

    _, email, _ = reviewer_factory(Role.REVIEWER)
    for _ in range(7):
        client.post("/api/v1/auth/login", json={"email": email, "password": "wrong-pw-here"})

    events = db.query(AuditEvent).filter(AuditEvent.action == "security.rate_limited").all()
    assert events, "rate-limit refusals must leave evidence"
    assert events[0].outcome == "denied"
    assert events[0].detail["retry_after"] > 0


def test_blocked_requests_do_not_reach_password_verification(client, reviewer_factory, monkeypatch):
    """Once blocked, no bcrypt work is done -- otherwise the limiter would not
    protect against CPU exhaustion, only against guessing."""
    _, email, _ = reviewer_factory(Role.REVIEWER)

    for _ in range(6):
        client.post("/api/v1/auth/login", json={"email": email, "password": "wrong-pw-here"})

    calls = {"n": 0}

    def _counting_verify(*args, **kwargs):
        calls["n"] += 1
        return False

    monkeypatch.setattr("backend.app.api.routes.auth.verify_password", _counting_verify)

    resp = client.post("/api/v1/auth/login", json={"email": email, "password": "wrong-pw-here"})
    assert resp.status_code == 429
    assert calls["n"] == 0, "password verification ran despite the block"


def test_a_successful_login_also_clears_the_ip_budget(client, reviewer_factory):
    """Signing in successfully is the opposite of the spraying the IP limiter
    exists to catch. Charging for it locks out a demo that cycles through roles,
    which is a real failure mode -- it happened during development."""
    accounts = [reviewer_factory(Role.REVIEWER) for _ in range(12)]

    # Twelve successful logins from one host: well past the budget if success
    # were charged for.
    for _user, email, password in accounts:
        resp = client.post("/api/v1/auth/login", json={"email": email, "password": password})
        assert resp.status_code == 200, "a successful login consumed the IP budget"


def test_successful_registrations_do_not_exhaust_the_budget(client, approve_email):
    """An admin onboarding a team must not lock themselves out with their own work."""
    for i in range(12):
        email = f"joiner{i}@example.com"
        approve_email(email)
        resp = client.post(
            "/api/v1/auth/register",
            json={"email": email, "password": "a-long-enough-password"},
        )
        assert resp.status_code == 201, f"onboarding stalled at #{i + 1}: {resp.text}"


def test_repeated_refusals_still_trip_the_limiter(client):
    """The protection has to survive the fix: failures must still accumulate."""
    statuses = [
        client.post(
            "/api/v1/auth/register",
            json={"email": f"probe{i}@example.com", "password": "a-long-enough-password"},
        ).status_code
        for i in range(25)
    ]
    assert 429 in statuses, "probing for approved addresses was not throttled"


def test_limiters_are_configured_conservatively():
    """Guards against someone loosening these to the point of uselessness."""
    # Per-account stays tight: that is the budget an attacker guessing one
    # password actually spends.
    assert login_account_limiter.max_attempts <= 5
    assert login_account_limiter.block_seconds >= 300
    # Per-IP and registration are wider, because successful attempts are refunded
    # and only failures accumulate there. Still bounded, though.
    assert login_ip_limiter.max_attempts <= 30
    assert register_ip_limiter.max_attempts <= 30
