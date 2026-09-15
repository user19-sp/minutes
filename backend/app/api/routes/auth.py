"""Authentication endpoints."""

from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException, status

from backend.app.api.deps import Audit, CurrentUser, DbSession, RequireAdmin
from backend.app.models import ActorType, Role, User
from backend.app.schemas import LoginRequest, TokenResponse, UserCreate, UserOut
from backend.app.security.auth import create_access_token, hash_password, verify_password

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
def register(payload: UserCreate, db: DbSession, audit: Audit) -> User:
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

    audit.record(
        action="auth.registered",
        actor_type=ActorType.HUMAN,
        actor_id=user.id,
        resource_type="user",
        resource_id=user.id,
        detail={"email": user.email, "role": user.role.value},
    )
    return user


@router.post("/login", response_model=TokenResponse, summary="Exchange credentials for a JWT")
def login(payload: LoginRequest, db: DbSession, audit: Audit) -> TokenResponse:
    started = time.perf_counter()
    user = db.query(User).filter(User.email == payload.email.lower()).one_or_none()

    if user is None:
        verify_password(payload.password, _DUMMY_HASH)  # constant-ish time
        audit.record(
            action="auth.login_failed",
            actor_type=ActorType.SYSTEM,
            actor_id=None,
            outcome="denied",
            detail={"reason": "unknown_email", "email": payload.email.lower()},
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect email or password."
        )

    if not user.is_active or not verify_password(payload.password, user.hashed_password):
        audit.record(
            action="auth.login_failed",
            actor_type=ActorType.HUMAN,
            actor_id=user.id,
            outcome="denied",
            resource_type="user",
            resource_id=user.id,
            detail={"reason": "bad_password" if user.is_active else "inactive_account"},
            duration_ms=(time.perf_counter() - started) * 1000,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect email or password."
        )

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
