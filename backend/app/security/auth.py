"""Password hashing, JWT issue/verify, and role-based access control."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import bcrypt
import jwt
from jwt import InvalidTokenError

from backend.app.config import settings
from backend.app.models import Role

# bcrypt silently truncates at 72 bytes; reject longer input rather than hash a prefix.
MAX_PASSWORD_BYTES = 72

ROLE_RANK: dict[Role, int] = {Role.VIEWER: 0, Role.REVIEWER: 1, Role.ADMIN: 2}


class AuthError(Exception):
    """Raised for any credential or token failure. Callers map this to HTTP 401."""


def hash_password(password: str) -> str:
    if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        raise ValueError(f"password must be at most {MAX_PASSWORD_BYTES} bytes")
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        # Malformed hash in the DB -- fail closed rather than raise.
        return False


def create_access_token(
    subject: str,
    role: Role,
    email: str,
    expires_minutes: int | None = None,
) -> tuple[str, int]:
    """Return (token, seconds_until_expiry)."""
    ttl = expires_minutes if expires_minutes is not None else settings.access_token_ttl_minutes
    now = datetime.now(UTC)
    expire = now + timedelta(minutes=ttl)
    payload: dict[str, Any] = {
        "sub": subject,
        "email": email,
        "role": role.value,
        "iat": int(now.timestamp()),
        "exp": int(expire.timestamp()),
        "jti": str(uuid.uuid4()),
        "iss": "mom-platform",
    }
    token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return token, ttl * 60


def decode_access_token(token: str) -> dict[str, Any]:
    try:
        return jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            issuer="mom-platform",
            options={"require": ["exp", "sub", "iss"]},
        )
    except InvalidTokenError as exc:
        raise AuthError(str(exc)) from exc


def role_allows(actual: Role, required: Role) -> bool:
    """Roles are ranked: viewer < reviewer < admin."""
    return ROLE_RANK[actual] >= ROLE_RANK[required]
