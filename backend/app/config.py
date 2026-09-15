"""Application settings, loaded from environment / .env (never from committed code)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = "development"
    log_level: str = "INFO"

    # Auth
    jwt_secret: str = "dev-only-insecure-secret-change-me"
    jwt_algorithm: str = "HS256"
    access_token_ttl_minutes: int = 60

    # Database
    database_url: str = "sqlite:///./mom.db"

    # Storage
    upload_dir: Path = PROJECT_ROOT / "backend" / "app" / "storage" / "uploads"
    export_dir: Path = PROJECT_ROOT / "backend" / "app" / "storage" / "exports"
    max_upload_mb: int = 200

    # Governance
    agent_max_tool_calls: int = 25
    # Confidence at/above which a proposal may skip the human gate.
    # Default > 1.0 means "nothing is ever auto-approved" — the safe default.
    agent_auto_approve_threshold: float = 1.01

    # Privacy
    pii_scrubbing_enabled: bool = True

    # Schema management
    # Dev and test create tables directly from the ORM metadata for convenience.
    # Deployments set this false and run `alembic upgrade head` instead, so schema
    # changes are versioned and reviewable rather than applied implicitly at
    # startup. Compose sets it false.
    auto_create_tables: bool = True

    # CORS
    # Held as a raw CSV string: pydantic-settings JSON-decodes complex types
    # straight from the environment, so a plain comma-separated value in .env
    # would fail to parse before any validator could normalise it.
    cors_origins_csv: str = Field(
        default="http://localhost:5173,http://127.0.0.1:5173",
        validation_alias="CORS_ORIGINS",
    )

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.cors_origins_csv.split(",") if o.strip()]

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() in {"production", "prod"}

    @property
    def allowed_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    settings.export_dir.mkdir(parents=True, exist_ok=True)
    return settings


settings = get_settings()
