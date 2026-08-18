from functools import lru_cache
from typing import Literal

from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Central application configuration, sourced from environment / .env.

    Nothing here is hardcoded elsewhere — services and routers pull settings
    through `get_settings()` so the whole app is configurable per-environment
    without code changes.
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- App metadata ---
    app_name: str = "FADE API"
    app_version: str = "0.1.0"
    environment: Literal["development", "test", "production"] = "development"
    debug: bool = True
    api_v1_prefix: str = "/api/v1"

    # --- Database ---
    # Defaults to a local SQLite file so the API runs with zero external
    # dependencies; point DATABASE_URL at Postgres (asyncpg) in production.
    database_url: str = "sqlite+aiosqlite:///./fade.db"
    database_echo: bool = False

    # --- Security / auth ---
    secret_key: str = Field(
        default="dev-only-insecure-secret-change-me-via-SECRET_KEY-env-var",
        description="HMAC signing key for JWTs. MUST be overridden in production.",
    )
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 60 * 12
    refresh_token_expire_minutes: int = 60 * 24 * 7

    # --- CORS ---
    cors_allow_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]

    # --- Rate limiting ---
    rate_limit_default: str = "120/minute"

    # --- Clinical thresholds (kept configurable, not magic numbers buried in services) ---
    uncertainty_review_threshold: float = 45.0

    # --- File uploads ---
    max_upload_mb: int = 64
    upload_dir: str = "./data/uploads"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
