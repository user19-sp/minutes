"""Shared FastAPI dependencies: authentication, authorisation, audit, ownership."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Path, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from backend.app.agent.audit import AuditLogger
from backend.app.db import get_db
from backend.app.models import Job, Role, User
from backend.app.observability.logging import user_id_ctx
from backend.app.security.auth import AuthError, decode_access_token, role_allows

bearer = HTTPBearer(auto_error=False, description="JWT from POST /api/v1/auth/login")

CREDENTIALS_ERROR = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Not authenticated.",
    headers={"WWW-Authenticate": "Bearer"},
)

DbSession = Annotated[Session, Depends(get_db)]


def get_audit(db: DbSession) -> AuditLogger:
    return AuditLogger(db)


Audit = Annotated[AuditLogger, Depends(get_audit)]


def get_current_user(
    db: DbSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)] = None,
) -> User:
    if credentials is None or not credentials.credentials:
        raise CREDENTIALS_ERROR

    try:
        payload = decode_access_token(credentials.credentials)
    except AuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid or expired token: {exc}",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    user = db.get(User, payload.get("sub"))
    if user is None or not user.is_active:
        raise CREDENTIALS_ERROR

    # The role is re-read from the database, never trusted from the token: a
    # demotion takes effect immediately rather than at token expiry.
    user_id_ctx.set(user.id)
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_role(minimum: Role):
    """Dependency factory enforcing the role ladder (viewer < reviewer < admin)."""

    def _dependency(user: CurrentUser) -> User:
        if not role_allows(user.role, minimum):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"This action requires the {minimum.value!r} role or higher; "
                    f"you have {user.role.value!r}."
                ),
            )
        return user

    return _dependency


RequireReviewer = Annotated[User, Depends(require_role(Role.REVIEWER))]
RequireAdmin = Annotated[User, Depends(require_role(Role.ADMIN))]


def get_owned_job(
    job_id: Annotated[str, Path(min_length=36, max_length=36)],
    db: DbSession,
    user: CurrentUser,
) -> Job:
    """Fetch a job, enforcing ownership.

    A non-owner gets 404 rather than 403 so the API does not confirm that another
    user's job id exists (IDOR probing). Admins may read any job.
    """
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")
    if job.owner_id != user.id and user.role is not Role.ADMIN:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")
    return job


OwnedJob = Annotated[Job, Depends(get_owned_job)]
