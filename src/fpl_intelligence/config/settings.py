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

    # --- Phase 13.5 — squad auto-sync ---------------------------------------
    # Public POST /api/v1/squad/retry-sync is rate-limited per client to avoid
    # hammering the upstream FPL API on deadline day.
    retry_sync_rate_limit: int = 10
    retry_sync_rate_window_seconds: int = 60

    # --- Phase 15.0 — real prediction chain ----------------------------------
    # Which DecisionPredictionProvider /api/v1/decisions resolves to:
    #   * "live"   — LivePredictionProvider fallback chain (backtest ->
    #                baseline model -> labelled pre-season proxy v2). Default.
    #   * "static" — StaticPredictionProvider hardcoded stub. Tests/dry-run
    #                ONLY; startup fails fast when app_env=production.
    prediction_provider: str = "live"
    # The Odds API key (optional free tier, https://the-odds-api.com). When
    # absent the market-check enrichment is disabled with a warning — never a
    # crash. Free tier: 500 credits/month; the connector caches 12h.
    the_odds_api_key: str = ""

    # --- Phase 18.0 — FPL egress mask chain --------------------------------
    # When the direct FPL fetch is blocked (403/429/500 on Vercel shared egress),
    # the importer tries CORS masks in order. The last mask is the user's
    # Google Apps Script proxy; set its base URL here. The importer appends
    # ?url=<encoded fpl url> itself, so this is the URL *up to and including*
    # the query param name, e.g. "https://script.google.com/macros/s/AKfycb…/exec".
    fpl_proxy_url: str = ""
    # Per-strategy network timeout for the egress chain (seconds).
    egress_strategy_timeout: float = 4.0
    # TTL for cached FPL responses (seconds).
    egress_cache_ttl: float = 60

    # --- Phase 19.0 — real-system sync trio ---------------------------------
    # Shared secret for machine-to-machine pushes (bookmarklet, Google Apps
    # Script fetcher, GitHub Actions data refresh). Callers authenticate with
    # ``Authorization: Bearer <SYNC_PUSH_TOKEN>``. Empty means "not configured":
    # every push endpoint then answers 503 so an unconfigured deployment can
    # never accept unauthenticated writes.
    sync_push_token: str = ""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
