from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str = "development"
    log_level: str = "INFO"
    database_url: str = "postgresql+psycopg://fpl:fpl@localhost:5432/fpl"
    fpl_base_url: str = "https://fantasy.premierleague.com"
    request_timeout_seconds: float = 20.0
    max_retries: int = 3

    # --- Phase 11.2 — frontend separation (Vercel / Netlify readiness) -----
    # When true (default), FastAPI also serves the static dashboard SPA. When
    # false, the app acts purely as a JSON API so the static assets can be
    # hosted on a separate CDN / Vercel deployment.
    serve_static_dashboard: bool = True
    # Comma-separated list of allowed CORS origins for a separate frontend.
    cors_origins: str = ""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
