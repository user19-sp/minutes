"""Rate limiting and brute-force protection (threat model R1).

Closes the highest-severity residual risk in docs/threat-model.md: without this,
online password guessing against `/auth/login` is unthrottled.

Two independent limiters, because they stop different attacks:

  * **Per-IP** -- one host spraying many accounts. Cheap and catches the noisy case.
  * **Per-account** -- a distributed attempt against one known email. An attacker
    with many IPs slips past a per-IP limit entirely, so the account itself also
    has a budget, and exhausting it locks the account briefly regardless of source.

**Only failures should cost you.** A successful attempt clears the counters it
would otherwise have consumed. This matters more than it sounds: a limiter that
charges for success punishes exactly the people it is meant to protect -- an
admin onboarding six colleagues in a morning, or a demo signing in as three
different roles in a row. Attackers are distinguished by *failing repeatedly*,
which is what the budget is actually measuring.

Storage is in-process, which is honest about what this is: correct for the
single-instance prototype, and wrong the moment there are two API replicas, since
each would keep its own counters. Redis is the swap, and `RateLimiter` is
deliberately small so that swap is contained. This limitation is recorded in the
threat model rather than hidden.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

__all__ = [
    "Decision",
    "RateLimitExceeded",
    "RateLimiter",
    "login_account_limiter",
    "login_ip_limiter",
    "register_ip_limiter",
]


class RateLimitExceeded(Exception):
    """Raised when a caller has exhausted its budget. Maps to HTTP 429."""

    def __init__(self, retry_after: int, scope: str) -> None:
        self.retry_after = retry_after
        self.scope = scope
        super().__init__(f"rate limit exceeded for {scope}; retry in {retry_after}s")


@dataclass
class Decision:
    allowed: bool
    remaining: int
    retry_after: int


@dataclass
class _Bucket:
    hits: list[float] = field(default_factory=list)
    blocked_until: float = 0.0


class RateLimiter:
    """Sliding-window counter with a cooldown once the budget is spent.

    A sliding window rather than a fixed one: fixed windows let an attacker send
    a full budget at the end of one window and another at the start of the next,
    doubling the effective rate at the boundary.
    """

    def __init__(
        self,
        *,
        max_attempts: int,
        window_seconds: int,
        block_seconds: int,
        name: str = "limiter",
    ) -> None:
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self.block_seconds = block_seconds
        self.name = name
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()
        self._last_sweep = 0.0

    # ------------------------------------------------------------------ api #

    def check(self, key: str) -> Decision:
        """Report the state of a key without consuming budget."""
        now = time.monotonic()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                return Decision(True, self.max_attempts, 0)
            if bucket.blocked_until > now:
                return Decision(False, 0, int(bucket.blocked_until - now) + 1)
            recent = self._recent(bucket, now)
            return Decision(True, max(0, self.max_attempts - len(recent)), 0)

    def hit(self, key: str) -> Decision:
        """Record one attempt. Raises `RateLimitExceeded` once the budget is gone."""
        now = time.monotonic()
        with self._lock:
            self._maybe_sweep(now)
            bucket = self._buckets.setdefault(key, _Bucket())

            if bucket.blocked_until > now:
                raise RateLimitExceeded(int(bucket.blocked_until - now) + 1, self.name)

            bucket.hits = self._recent(bucket, now)
            bucket.hits.append(now)

            if len(bucket.hits) > self.max_attempts:
                bucket.blocked_until = now + self.block_seconds
                # Drop the window: the cooldown now governs, and keeping the
                # timestamps would let the block re-arm the moment it expires.
                bucket.hits.clear()
                raise RateLimitExceeded(self.block_seconds, self.name)

            return Decision(True, self.max_attempts - len(bucket.hits), 0)

    def reset(self, key: str) -> None:
        """Clear a key. Called on successful login so honest users are not
        penalised for earlier typos."""
        with self._lock:
            self._buckets.pop(key, None)

    def clear(self) -> None:
        """Test hook."""
        with self._lock:
            self._buckets.clear()
            self._last_sweep = 0.0

    # ------------------------------------------------------------ internals #

    def _recent(self, bucket: _Bucket, now: float) -> list[float]:
        cutoff = now - self.window_seconds
        return [t for t in bucket.hits if t > cutoff]

    def _maybe_sweep(self, now: float) -> None:
        """Evict dead buckets periodically.

        Without this the dict grows once per distinct IP seen, which is itself a
        slow memory-exhaustion vector. Sweeping at most once a minute keeps the
        cost off the hot path.
        """
        if now - self._last_sweep < 60:
            return
        self._last_sweep = now
        cutoff = now - self.window_seconds
        dead = [
            key
            for key, bucket in self._buckets.items()
            if bucket.blocked_until <= now and not [t for t in bucket.hits if t > cutoff]
        ]
        for key in dead:
            del self._buckets[key]


# --------------------------------------------------------------------------- #
# Configured limiters
# --------------------------------------------------------------------------- #

#: One host, many *failed* attempts. Generous enough for a shared office NAT and
#: for a demo cycling through roles; tight enough that guessing is impractical.
#: Successful logins are cleared, so only failures accumulate here.
login_ip_limiter = RateLimiter(
    max_attempts=20,
    window_seconds=300,
    block_seconds=900,
    name="login-ip",
)

#: One account, from anywhere. Stops a distributed attempt on a known email.
login_account_limiter = RateLimiter(
    max_attempts=5,
    window_seconds=300,
    block_seconds=900,
    name="login-account",
)

#: Registration. The registration allow-list is now the real control over who
#: gets an account, so this exists to stop someone hammering the endpoint to
#: discover *which* addresses are approved. Successful registrations are cleared,
#: so an admin onboarding a team does not lock themselves out -- only refusals
#: accumulate. The block is short for the same reason.
register_ip_limiter = RateLimiter(
    max_attempts=20,
    window_seconds=900,
    block_seconds=900,
    name="register-ip",
)


def client_ip(request) -> str:  # type: ignore[no-untyped-def]
    """Best-effort client address.

    `X-Forwarded-For` is only consulted because the deployment puts nginx in
    front of the API. A spoofable header must never be trusted for authorisation
    -- it is used here solely to bucket rate-limit counters, where the worst case
    is an attacker fragmenting their own budget. The per-account limiter is what
    actually holds in that case.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"
