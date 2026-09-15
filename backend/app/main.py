"""FastAPI application entrypoint."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import text

from backend.app.api.routes import approvals, audit, auth, exports, jobs, minutes, runs
from backend.app.config import settings
from backend.app.db import SessionLocal, engine, init_db
from backend.app.observability.logging import configure_logging, get_logger
from backend.app.observability.metrics import registry as metrics_registry
from backend.app.observability.middleware import (
    BodySizeLimitMiddleware,
    RequestContextMiddleware,
)
from backend.app.schemas import HealthOut

API_PREFIX = "/api/v1"
VERSION = "0.1.0"

# Starlette renamed 422 to UNPROCESSABLE_CONTENT and deprecated the old spelling.
# Resolved once here so the app works on either version. Note the hasattr guard:
# a getattr() default is evaluated eagerly, which would touch the deprecated
# attribute -- and emit the warning -- even on versions that have the new name.
HTTP_422 = (
    status.HTTP_422_UNPROCESSABLE_CONTENT
    if hasattr(status, "HTTP_422_UNPROCESSABLE_CONTENT")
    else status.HTTP_422_UNPROCESSABLE_ENTITY
)

configure_logging()
log = get_logger("app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Importing the tool module is what populates the allow-list; do it explicitly
    # at startup so the registry is never half-built when the first request lands.
    from backend.app.agent import tools  # noqa: F401
    from backend.app.agent.registry import registry as tool_registry

    init_db()

    with SessionLocal() as db:
        from backend.app.agent.gates import recount_pending

        pending = recount_pending(db)

    if settings.is_production and settings.jwt_secret.startswith("dev-only"):
        raise RuntimeError(
            "JWT_SECRET is still the development default. Set a real secret before "
            "running in production."
        )

    log.info(
        "startup",
        version=VERSION,
        environment=settings.app_env,
        database=settings.database_url.split("://")[0],
        registered_tools=sorted(tool_registry.names()),
        gated_tools=sorted(s.name for s in tool_registry.specs() if s.requires_approval),
        pending_approvals=pending,
        pii_scrubbing=settings.pii_scrubbing_enabled,
        schema_mode="orm_create_all" if settings.auto_create_tables else "alembic",
    )
    yield
    log.info("shutdown")


app = FastAPI(
    title="Multilingual Meeting Intelligence Agent",
    description=(
        "Human-governed meeting intelligence: audio to reviewed, exportable minutes.\n\n"
        "Every action the agent can take is on a published allow-list, every action "
        "with a side effect stops at a human approval gate, and every step is "
        "recorded in an append-only audit trail."
    ),
    version=VERSION,
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)

# --------------------------------------------------------------------------- #
# Middleware (outermost first)
# --------------------------------------------------------------------------- #

app.add_middleware(RequestContextMiddleware)
app.add_middleware(BodySizeLimitMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Trace-Id"],
    expose_headers=["X-Trace-Id", "X-Content-SHA256"],
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """Baseline hardening headers on every response."""
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Cache-Control", "no-store")
    if settings.is_production:
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )
    return response


# --------------------------------------------------------------------------- #
# Exception handlers
# --------------------------------------------------------------------------- #


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Return field-level errors without echoing the submitted body back.

    Echoing input is how a validation error turns into a reflection gadget; the
    client already knows what it sent.
    """
    return JSONResponse(
        status_code=HTTP_422,
        content={
            "detail": "Request validation failed.",
            "errors": [
                {
                    "field": ".".join(str(p) for p in e["loc"]),
                    "message": e["msg"],
                    "type": e["type"],
                }
                for e in exc.errors()
            ],
            "trace_id": getattr(request.state, "trace_id", None),
        },
    )


@app.exception_handler(Exception)
async def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
    """Never leak a stack trace to the client; log it with the trace id instead."""
    trace_id = getattr(request.state, "trace_id", None)
    log.exception(
        "unhandled_exception",
        path=request.url.path,
        error_type=type(exc).__name__,
        error=str(exc),
    )
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "detail": "Internal server error.",
            "trace_id": trace_id,
            "hint": "Quote the trace_id when reporting this.",
        },
    )


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #

app.include_router(auth.router, prefix=API_PREFIX)
app.include_router(jobs.router, prefix=API_PREFIX)
app.include_router(runs.router, prefix=API_PREFIX)
app.include_router(approvals.router, prefix=API_PREFIX)
app.include_router(minutes.router, prefix=API_PREFIX)
app.include_router(exports.router, prefix=API_PREFIX)
app.include_router(audit.router, prefix=API_PREFIX)


@app.get("/health", response_model=HealthOut, tags=["ops"], summary="Liveness and readiness")
def health() -> HealthOut:
    db_state = "ok"
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:
        db_state = f"error: {type(exc).__name__}"
        log.error("health_db_check_failed", error=str(exc))

    return HealthOut(
        status="ok" if db_state == "ok" else "degraded",
        version=VERSION,
        environment=settings.app_env,
        database=db_state,
    )


@app.get("/metrics", tags=["ops"], summary="Prometheus metrics")
def metrics() -> PlainTextResponse:
    return PlainTextResponse(
        generate_latest(metrics_registry).decode("utf-8"), media_type=CONTENT_TYPE_LATEST
    )


@app.get("/", tags=["ops"], summary="Service banner")
def root() -> dict:
    return {
        "service": "Multilingual Meeting Intelligence Agent",
        "version": VERSION,
        "docs": "/docs",
        "health": "/health",
        "metrics": "/metrics",
        "api": API_PREFIX,
    }
