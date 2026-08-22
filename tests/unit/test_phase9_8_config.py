"""Phase 9.8 unit tests — Production Configuration.

Every test mocks the configuration file (``tmp_path`` YAML) and the environment
(``environ=...``) so no real ``os.environ`` or ``.env`` is ever consulted. Secrets
are asserted to be env-only and to be redacted in serialised output.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from fpl_intelligence.deployment.config import (
    CONFIG_SCHEMA_VERSION,
    PRODUCTION_ENV,
    SECRET_FIELDS,
    ProductionConfig,
    ProductionConfigError,
    load_production_config,
    validate_production_config,
)


def _write_config(tmp_path: Path, data: dict[str, Any]) -> Path:
    path = tmp_path / "production.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_load_uses_defaults_for_missing_file(tmp_path: Path) -> None:
    config = load_production_config(tmp_path / "missing.yaml", environ={})
    assert config.app_env == PRODUCTION_ENV
    assert config.port == 8000
    assert config.workers == 2
    assert config.database_url.startswith("postgresql+psycopg://")


def test_load_reads_yaml_values(tmp_path: Path) -> None:
    path = _write_config(tmp_path, {"port": 9000, "workers": 4, "log_level": "DEBUG"})
    config = load_production_config(path, environ={})
    assert config.port == 9000
    assert config.workers == 4
    assert config.log_level == "DEBUG"


def test_env_overrides_yaml(tmp_path: Path) -> None:
    path = _write_config(tmp_path, {"port": 9000, "workers": 4})
    config = load_production_config(path, environ={"PORT": "8080"})
    assert config.port == 8080
    assert config.workers == 4  # not overridden by env


def test_secrets_only_from_env(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path, {"slack_webhook_url": "file://ignored-secret", "smtp_password": "file-pass"}
    )
    config = load_production_config(
        path, environ={"SLACK_WEBHOOK_URL": "https://hooks.example.com"}
    )
    assert config.slack_webhook_url == "https://hooks.example.com"
    assert config.smtp_password is None


def test_yaml_secret_value_is_ignored(tmp_path: Path) -> None:
    path = _write_config(tmp_path, {"critical_error_webhook_url": "https://file-only"})
    config = load_production_config(path, environ={})
    assert config.critical_error_webhook_url is None


def test_redaction_masks_secrets_in_to_dict() -> None:
    config = ProductionConfig(
        app_env=PRODUCTION_ENV,
        slack_webhook_url="https://secret",
        smtp_password="hunter2",
        critical_error_webhook_url="https://critical",
    )
    dumped = config.to_dict(redact_secrets=True)
    assert dumped["slack_webhook_url"] == "***"
    assert dumped["smtp_password"] == "***"
    assert dumped["critical_error_webhook_url"] == "***"


def test_to_dict_without_redaction_contains_real_values() -> None:
    config = ProductionConfig(app_env=PRODUCTION_ENV, smtp_password="hunter2")
    dumped = config.to_dict(redact_secrets=False)
    assert dumped["smtp_password"] == "hunter2"


def test_to_dict_never_contains_secret_values_by_default() -> None:
    config = ProductionConfig(
        app_env=PRODUCTION_ENV,
        slack_webhook_url="https://secret",
        smtp_username="creds-user",
        smtp_password="creds-pass",
        critical_error_webhook_url="https://critical",
    )
    dumped = config.to_dict()
    for field_name in SECRET_FIELDS:
        assert dumped[field_name] == "***"


def test_app_env_forced_to_production(tmp_path: Path) -> None:
    path = _write_config(tmp_path, {"app_env": "development"})
    config = load_production_config(path, environ={"APP_ENV": "development"})
    assert config.app_env == PRODUCTION_ENV


def test_validation_ok_for_production_defaults(tmp_path: Path) -> None:
    config = load_production_config(tmp_path / "nope.yaml", environ={})
    assert validate_production_config(config) == []


def test_validation_rejects_sqlite_database_url(tmp_path: Path) -> None:
    path = _write_config(tmp_path, {"database_url": "sqlite:///./fpl.db"})
    config = load_production_config(path, environ={})
    issues = validate_production_config(config)
    assert any("PostgreSQL" in issue for issue in issues)


def test_field_validator_rejects_bad_port() -> None:
    with pytest.raises(ValidationError):
        ProductionConfig(app_env=PRODUCTION_ENV, port=99999)


def test_field_validator_rejects_zero_workers() -> None:
    with pytest.raises(ValidationError):
        ProductionConfig(app_env=PRODUCTION_ENV, workers=0)


def test_field_validator_rejects_zero_retries() -> None:
    with pytest.raises(ValidationError):
        ProductionConfig(app_env=PRODUCTION_ENV, max_retries=0)


def test_field_validator_rejects_low_multiplier() -> None:
    with pytest.raises(ValidationError):
        ProductionConfig(app_env=PRODUCTION_ENV, retry_multiplier=0.5)


def test_schema_version_mismatch_rejected() -> None:
    with pytest.raises(ValidationError):
        ProductionConfig(app_env=PRODUCTION_ENV, schema_version=CONFIG_SCHEMA_VERSION + 1)


def test_invalid_yaml_raises_config_error(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("  - this is\n  : : invalid  yaml: [", encoding="utf-8")
    with pytest.raises(ProductionConfigError):
        load_production_config(path, environ={})


def test_non_mapping_yaml_raises_config_error(tmp_path: Path) -> None:
    path = tmp_path / "list.yaml"
    path.write_text("- a\n- b\n- c\n", encoding="utf-8")
    with pytest.raises(ProductionConfigError):
        load_production_config(path, environ={})


def test_real_production_config_file_is_secret_free() -> None:
    config = load_production_config(Path("config/production.yaml"), environ={})
    assert config.app_env == PRODUCTION_ENV
    assert config.database_url.startswith("postgresql")
    assert config.slack_webhook_url is None
    assert config.smtp_password is None
    assert config.critical_error_webhook_url is None


def test_load_is_deterministic(tmp_path: Path) -> None:
    path = _write_config(tmp_path, {"port": 7000, "workers": 3})
    first = load_production_config(path, environ={"LOG_LEVEL": "WARNING"})
    second = load_production_config(path, environ={"LOG_LEVEL": "WARNING"})
    assert first.to_dict() == second.to_dict()


def test_notify_channel_list_and_connectors(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path,
        {"notify_channels": "slack,email", "ingest_connectors": "rss, fpl_api"},
    )
    config = load_production_config(path, environ={})
    assert config.notify_channel_list == ["slack", "email"]
    assert config.ingest_connector_list == ["rss", "fpl_api"]
