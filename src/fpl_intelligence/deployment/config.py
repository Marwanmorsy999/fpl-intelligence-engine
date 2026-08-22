"""Phase 9.8 — Production Configuration.

The production configuration drives the deployed system: the API host/port, the
database, the live-ingestion scheduler (Phase 9.5/9.6), notification channels,
resilience knobs (Phase 9.8) and the Docker build parameters (Phase 9.8).

Design rules:

* **The config file carries no secrets.** Sensitive values (Slack webhook URL,
  SMTP credentials, the operational alert webhook) are **only** ever read from
  environment variables — a value written into the YAML file is ignored.
* **Environment variables override the file.** Every field maps to an
  ``UPPER_SNAKE`` environment variable (see :data:`ENV_FIELD_MAP`).
* **Fully mockable.** :func:`load_production_config` accepts an explicit
  ``environ`` mapping and an explicit file ``path``, so tests point it at a
  temporary YAML file with a fake environment — the real ``os.environ`` and
  ``.env`` are never touched when ``environ`` is supplied.
* **Deterministic.** The loader explicitly resolves every field from
  (env overrides + file + defaults) and forces ``app_env`` to ``production``,
  so the same inputs always produce the same config and ``.env``/``os.environ``
  cannot leak unrelated values into a deployment.
* **Validated before use.** :func:`validate_production_config` enforces the
  production constraints (PostgreSQL only, sane retry/breaker bounds) and feeds
  the deployment readiness report.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]
from pydantic import ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PRODUCTION_ENV = "production"
DEFAULT_CONFIG_PATH = Path("config/production.yaml")
CONFIG_SCHEMA_VERSION = 1

#: Fields that must NEVER be populated from the configuration file. They are
#: read from environment variables only, satisfying the "no secrets in the
#: repo / sensitive values via env vars" constraint.
SECRET_FIELDS: frozenset[str] = frozenset(
    {
        "slack_webhook_url",
        "smtp_username",
        "smtp_password",
        "critical_error_webhook_url",
    }
)

#: Explicit field -> environment-variable name mapping. Fields not listed here
#: use ``FIELD.upper()`` (pydantic-settings' default convention).
ENV_FIELD_MAP: dict[str, str] = {
    "app_env": "APP_ENV",
    "database_url": "DATABASE_URL",
    "fpl_base_url": "FPL_BASE_URL",
    "log_level": "LOG_LEVEL",
    "request_timeout_seconds": "REQUEST_TIMEOUT_SECONDS",
    "max_retries": "MAX_RETRIES",
    "slack_webhook_url": "SLACK_WEBHOOK_URL",
    "email_from": "EMAIL_FROM",
    "email_to": "EMAIL_TO",
    "smtp_host": "SMTP_HOST",
    "smtp_port": "SMTP_PORT",
    "smtp_username": "SMTP_USERNAME",
    "smtp_password": "SMTP_PASSWORD",
    "critical_error_webhook_url": "CRITICAL_ERROR_WEBHOOK_URL",
}


class ProductionConfigError(RuntimeError):
    """Raised when the production configuration file cannot be loaded."""


class ProductionConfig(BaseSettings):
    """All settings the deployed system needs.

    ``app_env`` is locked to ``"production"``: the deployment layer must never
    accidentally run with a development flag. ``_env_file=None`` disables
    ``.env`` loading; the loader resolves every field explicitly so the real
    environment cannot leak values into a deployment.
    """

    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    # -- identity / API ------------------------------------------------------
    schema_version: int = CONFIG_SCHEMA_VERSION
    app_env: str = PRODUCTION_ENV
    app_name: str = "fpl-intelligence-engine"
    host: str = "0.0.0.0"
    port: int = 8000
    workers: int = 2
    log_level: str = "INFO"
    database_url: str = "postgresql+psycopg://fpl:fpl@localhost:5432/fpl"
    fpl_base_url: str = "https://fantasy.premierleague.com"
    request_timeout_seconds: float = 20.0

    # -- live ingestion (Phase 9.5/9.6) --------------------------------------
    scheduler_enabled: bool = True
    scheduler_interval_seconds: float = 1800.0
    ingest_connectors: str = "rss,fpl_api"
    rss_url: str = "https://www.bbc.co.uk/sport/football/teams/rss"
    fpl_api_url: str = "https://fantasy.premierleague.com/api/bootstrap-static/"

    # -- notification channels (secrets env-only) ----------------------------
    notify_channels: str = "log"
    slack_webhook_url: str | None = None
    email_from: str | None = None
    email_to: str | None = None
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = None

    # -- resilience (Phase 9.8) ----------------------------------------------
    max_retries: int = 3
    retry_base_delay_seconds: float = 1.0
    retry_max_delay_seconds: float = 30.0
    retry_multiplier: float = 2.0
    circuit_breaker_failure_threshold: int = 5
    circuit_breaker_reset_seconds: float = 60.0

    # -- monitoring (Phase 9.8) ----------------------------------------------
    metrics_enabled: bool = True
    health_check_interval_seconds: float = 60.0
    critical_error_webhook_url: str | None = None

    # -- Docker build (Phase 9.8) --------------------------------------------
    docker_image_name: str = "fpl-intelligence-engine"
    docker_tag: str = "production"
    docker_context_path: str = "."
    dockerfile_path: str = "Dockerfile"
    # -- derived helpers --------------------------------------------------------

    @property
    def notify_channel_list(self) -> list[str]:
        """Channels to deliver Phase 9.6 alerts to (log / slack / email)."""
        return [c.strip() for c in self.notify_channels.split(",") if c.strip()]

    @property
    def ingest_connector_list(self) -> list[str]:
        """Connectors the Phase 9.6 scheduler should run each pass."""
        return [c.strip() for c in self.ingest_connectors.split(",") if c.strip()]

    def to_dict(self, *, redact_secrets: bool = True) -> dict[str, Any]:
        """Serialise the config, masking secrets unless explicitly requested.

        Used by the readiness report and deploy CLI so secrets never reach
        logs or terminal output.
        """
        data = self.model_dump()
        if redact_secrets:
            for field_name in SECRET_FIELDS:
                if data.get(field_name) is not None:
                    data[field_name] = "***"
        return data

    # -- validation --------------------------------------------------------

    @field_validator("app_env")
    @classmethod
    def _app_env_must_be_production(cls, value: str) -> str:
        if value != PRODUCTION_ENV:
            raise ValueError(f"app_env must be {PRODUCTION_ENV!r}")
        return value

    @field_validator("port")
    @classmethod
    def _port_in_range(cls, value: int) -> int:
        if not 1 <= value <= 65535:
            raise ValueError("port must be within 1..65535")
        return value

    @field_validator("workers", "max_retries", "circuit_breaker_failure_threshold")
    @classmethod
    def _at_least_one(cls, value: int, info: ValidationInfo) -> int:
        if value < 1:
            raise ValueError(f"{info.field_name} must be at least 1")
        return value

    @field_validator(
        "retry_base_delay_seconds",
        "retry_max_delay_seconds",
        "circuit_breaker_reset_seconds",
        "scheduler_interval_seconds",
        "health_check_interval_seconds",
        "request_timeout_seconds",
    )
    @classmethod
    def _non_negative(cls, value: float, info: ValidationInfo) -> float:
        if value < 0:
            raise ValueError(f"{info.field_name} must not be negative")
        return value

    @field_validator("retry_multiplier")
    @classmethod
    def _multiplier_floor(cls, value: float) -> float:
        if value < 1.0:
            raise ValueError("retry_multiplier must be at least 1.0")
        return value

    @field_validator("schema_version")
    @classmethod
    def _schema_version_supported(cls, value: int) -> int:
        if value != CONFIG_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported schema_version {value}; expected {CONFIG_SCHEMA_VERSION}"
            )
        return value


def _read_config_file(path: Path) -> dict[str, Any]:
    """Read a YAML mapping from ``path``. Missing file -> empty mapping."""
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ProductionConfigError(f"cannot read config file {path}: {exc}") from exc
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ProductionConfigError(f"invalid YAML in config file {path}: {exc}") from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ProductionConfigError(
            f"config file {path} must contain a YAML mapping, got {type(loaded).__name__}"
        )
    return {key: value for key, value in loaded.items() if isinstance(key, str)}


def load_production_config(
    path: Path | str = DEFAULT_CONFIG_PATH,
    environ: Mapping[str, str] | None = None,
) -> ProductionConfig:
    """Load the production configuration from a YAML file + environment.

    Resolution order per field: environment variable -> file value -> default.
    Secret fields are **never** taken from the file. ``app_env`` is always forced to
    ``production``. When ``environ`` is supplied, ``os.environ`` is not consulted at
    all (this is the seam tests use to mock the environment).
    """
    env = dict(os.environ if environ is None else environ)
    data = _read_config_file(Path(path))

    merged: dict[str, Any] = {}
    for field_name in ProductionConfig.model_fields:
        env_name = ENV_FIELD_MAP.get(field_name, field_name.upper())
        if env_name in env:
            merged[field_name] = env[env_name]
        elif field_name not in SECRET_FIELDS and field_name in data:
            merged[field_name] = data[field_name]
    merged["app_env"] = PRODUCTION_ENV

    try:
        return ProductionConfig(**merged)
    except Exception as exc:  # noqa: BLE001 - surface as a config-loading error
        raise ProductionConfigError(f"invalid production configuration: {exc}") from exc


def validate_production_config(config: ProductionConfig) -> list[str]:
    """Return a list of production-constraint violations (empty == ready).

    pydantic field validators already guarantee ranges; this adds the cross-cutting
    constraints used by the deployment readiness report.
    """
    issues: list[str] = []
    if config.app_env != PRODUCTION_ENV:
        issues.append(f"app_env must be {PRODUCTION_ENV!r}")
    if not config.database_url.startswith(("postgresql", "postgres")):
        issues.append(
            "production requires a PostgreSQL DATABASE_URL "
            f"(got {config.database_url.split('://', 1)[0]!r})"
        )
    if config.retry_max_delay_seconds < config.retry_base_delay_seconds:
        issues.append("retry_max_delay_seconds must be >= retry_base_delay_seconds")
    return issues


@lru_cache
def get_production_config() -> ProductionConfig:
    """Process-wide cached production config (reads ``os.environ`` + the file)."""
    return load_production_config()
