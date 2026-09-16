"""Authentication endpoints."""

from __future__ import annotations

import time
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Request, status

from backend.app.api.deps import Audit, CurrentUser, DbSession, RequireAdmin
from backend.app.config import settings
from backend.app.models import ActorType, ApprovedEmail, Role, User
from backend.app.schemas import (
    ApprovedEmailCreate,
    ApprovedEmailOut,
    LoginRequest,
    MessageOut,
    TokenResponse,
    UserCreate,
    UserOut,
)
from backend.app.security.auth import create_access_token, hash_password, verify_password
from backend.app.security.ratelimit import (
    RateLimitExceeded,
    client_ip,
    login_account_limiter,
    login_ip_limiter,
    register_ip_limiter,
)
from backend.app.services import approvals

router = APIRouter(prefix="/auth", tags=["auth"])

#: Cost of one bcrypt verification, burned on unknown emails so that response
#: time does not reveal whether an account exists.
_DUMMY_HASH = "$2b$12$abcdefghijklmnopqrstuvOe0Q6Zc0N1uVhWxJ5Jz5Y8kLmNqRsTu"


@router.post(
    "/register",
    response_model=UserOut,
    status_code=status.HTTP_201_CREATED,
    summary="Register a reviewer account",
)
def register(payload: UserCreate, request: Request, db: DbSession, audit: Audit) -> User:
    _enforce(register_ip_limiter, client_ip(request), audit, reason="register_rate_limited")

    # The threat model assumes no public registration; this is what enforces it.
    if not approvals.is_allowed(db, payload.email):
        approvals.record_refusal(db, audit, payload.email)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "That email address is not approved for registration. "
                "Ask an administrator to add it."
            ),
        )

    existing = db.query(User).filter(User.email == payload.email.lower()).one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="An account with that email exists."
        )

    # Self-registration cannot grant admin; only an existing admin may do that.
    requested = payload.role if payload.role is not Role.ADMIN else Role.REVIEWER

    user = User(
        email=payload.email.lower(),
        full_name=payload.full_name,
        hashed_password=hash_password(payload.password),
        role=requested,
    )
    db.add(user)
    db.flush()

    # A successful registration is not abuse: clear the budget it consumed so an
    # admin onboarding several people in a row is not locked out by their own work.
    register_ip_limiter.reset(client_ip(request))

    approvals.mark_used(db, user.email)
    audit.record(
        action="auth.registered",
        actor_type=ActorType.HUMAN,
        actor_id=user.id,
        resource_type="user",
        resource_id=user.id,
        detail={"email": user.email, "role": user.role.value},
    )
    return user


# --------------------------------------------------------------------------- #
# Registration allow-list (admin)
# --------------------------------------------------------------------------- #


@router.get(
    "/approved-emails",
    response_model=list[ApprovedEmailOut],
    summary="Addresses cleared to register (admin only)",
)
def list_approved_emails(db: DbSession, _: RequireAdmin) -> list[ApprovedEmail]:
    return approvals.list_approved(db)


@router.post(
    "/approved-emails",
    response_model=ApprovedEmailOut,
    status_code=status.HTTP_201_CREATED,
    summary="Approve an address for registration (admin only)",
)
def approve_email(
    payload: ApprovedEmailCreate, db: DbSession, admin: RequireAdmin, audit: Audit
) -> ApprovedEmail:
    try:
        return approvals.approve(db, audit, email=payload.email, admin=admin, note=payload.note)
    except approvals.AlreadyApproved as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.delete(
    "/approved-emails/{entry_id}",
    response_model=MessageOut,
    summary="Withdraw approval (admin only)",
)
def revoke_email(
    entry_id: Annotated[str, Path(min_length=36, max_length=36)],
    db: DbSession,
    admin: RequireAdmin,
    audit: Audit,
) -> MessageOut:
    entry = db.get(ApprovedEmail, entry_id)
    if entry is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not on the list.")

    address = entry.email
    approvals.revoke(db, audit, entry=entry, admin=admin)
    return MessageOut(
        detail=(
            f"{address} can no longer register. Any account already created with it "
            "still works -- deactivate the user to stop that."
        )
    )


@router.get("/registration-policy", summary="How registration is gated")
def registration_policy(db: DbSession) -> dict:
    """Public, so the login screen can explain itself rather than just refusing."""
    return {
        "mode": settings.registration_mode,
        "self_service": settings.registration_mode == "open",
        "message": (
            "Anyone may register."
            if settings.registration_mode == "open"
            else "Registration is limited to addresses an administrator has approved."
        ),
    }


@router.get("/demo", summary="Whether one-click demo sign-in is available")
def demo_availability(db: DbSession) -> dict:
    """Tells the login screen whether to offer a demo button.

    Availability needs two things to be true: demo mode is enabled, and the
    seeded account actually exists. Checking the second is the point -- on a fresh
    database where `scripts/seed_demo.py` has not been run, the button would
    otherwise appear and then fail, which is exactly the confusing outcome it is
    supposed to prevent.

    Credentials are returned only when available. They are not a secret: they are
    published in the README and created by a script that never runs in a real
    deployment. Serving them from here keeps one source of truth instead of the
    frontend holding a second copy that can drift.
    """
    if not settings.demo_mode:
        return {"available": False, "reason": "demo mode is disabled"}

    user = db.query(User).filter(User.email == settings.demo_email.lower()).one_or_none()
    if user is None or not user.is_active:
        return {
            "available": False,
            "reason": "no demo account -- run: python scripts/seed_demo.py",
        }

    return {
        "available": True,
        "email": settings.demo_email,
        "password": settings.demo_password,
        "role": user.role.value,
    }


@router.post("/login", response_model=TokenResponse, summary="Exchange credentials for a JWT")
def login(payload: LoginRequest, request: Request, db: DbSession, audit: Audit) -> TokenResponse:
    started = time.perf_counter()
    email = payload.email.lower()
    ip = client_ip(request)

    # Both budgets are checked before any password work: an attacker must not be
    # able to spend server CPU on bcrypt once they are already blocked.
    _enforce(login_ip_limiter, ip, audit, reason="login_rate_limited_ip")
    _enforce(login_account_limiter, email, audit, reason="login_rate_limited_account", email=email)

    user = db.query(User).filter(User.email == email).one_or_none()

    if user is None:
        verify_password(payload.password, _DUMMY_HASH)  # constant-ish time
        audit.refusal(
            "auth.login_failed",
            ActorType.SYSTEM,
            actor_id=None,
            detail={"reason": "unknown_email", "email": email},
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect email or password."
        )

    if not user.is_active or not verify_password(payload.password, user.hashed_password):
        audit.refusal(
            "auth.login_failed",
            ActorType.HUMAN,
            actor_id=user.id,
            resource_type="user",
            resource_id=user.id,
            detail={"reason": "bad_password" if user.is_active else "inactive_account"},
            duration_ms=(time.perf_counter() - started) * 1000,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect email or password."
        )

    # A successful login clears both budgets it consumed. The account budget so a
    # user who mistypes twice is not one attempt from lockout; the IP budget
    # because signing in successfully is the opposite of the spraying that limiter
    # exists to catch -- and a demo cycling through roles would otherwise trip it.
    login_account_limiter.reset(email)
    login_ip_limiter.reset(ip)

    token, expires_in = create_access_token(user.id, user.role, user.email)
    audit.record(
        action="auth.login",
        actor_type=ActorType.HUMAN,
        actor_id=user.id,
        resource_type="user",
        resource_id=user.id,
        detail={"role": user.role.value},
        duration_ms=(time.perf_counter() - started) * 1000,
    )
    return TokenResponse(
        access_token=token, expires_in=expires_in, user=UserOut.model_validate(user)
    )


@router.get("/me", response_model=UserOut, summary="Current user")
def me(user: CurrentUser) -> User:
    return user


@router.get(
    "/users",
    response_model=list[UserOut],
    summary="List accounts (admin only)",
)
def list_users(db: DbSession, _: RequireAdmin) -> list[User]:
    return db.query(User).order_by(User.created_at).all()


def _enforce(limiter, key: str, audit: Audit, *, reason: str, email: str | None = None) -> None:
    """Spend one unit of budget, or refuse with 429 and a Retry-After header.

    The refusal is audited: a burst of these is the signal that someone is being
    attacked, and it is exactly the evidence the threat model asks for.
    """
    try:
        limiter.hit(key)
    except RateLimitExceeded as exc:
        audit.refusal(
            "security.rate_limited",
            ActorType.SYSTEM,
            actor_id=None,
            detail={
                "reason": reason,
                "scope": exc.scope,
                "retry_after": exc.retry_after,
                **({"email": email} if email else {}),
            },
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=("Too many attempts. Please wait before trying again."),
            headers={"Retry-After": str(exc.retry_after)},
        ) from exc
