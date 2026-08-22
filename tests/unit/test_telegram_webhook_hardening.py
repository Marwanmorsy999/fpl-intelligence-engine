"""Hotfix v1.2.2 — Telegram webhook hardening found during live verification.

Two live defects are locked down here:

1. ``POST /api/v1/telegram/webhook`` with a wrong secret answered **500**. The
   route was annotated ``-> dict[str, bool]`` while ``handle_webhook`` reports
   rejections as ``{"ok": False, "error": "secret mismatch"}``, so FastAPI raised
   ``ResponseValidationError`` on the string detail. A 5xx also makes Telegram
   retry, turning a rejected probe into repeated invocations.
2. ``httpx`` logs the full Telegram API URL at INFO level, which embeds the bot
   token, so the credential was written verbatim into the platform log stream.

Everything here runs offline: no Telegram client is built and no request leaves
the process.
"""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from fpl_intelligence.common.logging import (
    CREDENTIAL_LEAKING_LOGGERS,
    configure_logging,
    silence_credential_leaking_loggers,
)

VALID_UPDATE = {
    "update_id": 1,
    "message": {
        "message_id": 1,
        "date": 1787236800,
        "chat": {"id": 4242, "type": "private"},
        "from": {"id": 4242, "is_bot": False, "first_name": "Tester"},
        "text": "/start",
    },
}


@pytest.fixture
def client() -> TestClient:
    from fpl_intelligence.api.main import app

    with TestClient(app) as test_client:
        yield test_client


# --------------------------------------------------------------------------- #
# 1. Wrong secret must be a controlled rejection, never a 500
# --------------------------------------------------------------------------- #
def test_wrong_secret_is_rejected_without_a_server_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "the-real-secret")

    resp = client.post("/api/v1/telegram/webhook?secret=deliberately-invalid", json=VALID_UPDATE)

    assert resp.status_code == 200, f"expected a clean rejection, got {resp.text}"
    assert resp.json() == {"ok": False, "error": "secret mismatch"}


def test_missing_secret_is_rejected_without_a_server_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "the-real-secret")

    resp = client.post("/api/v1/telegram/webhook", json=VALID_UPDATE)

    assert resp.status_code == 200
    assert resp.json()["ok"] is False


def test_invalid_payload_is_reported_not_crashed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "the-real-secret")

    resp = client.post(
        "/api/v1/telegram/webhook?secret=the-real-secret",
        content=b"{not json",
        headers={"Content-Type": "application/json"},
    )

    assert resp.status_code == 200
    assert resp.json() == {"ok": False, "error": "invalid payload"}


def test_webhook_response_model_accepts_string_details() -> None:
    """The route must not narrow its response to ``dict[str, bool]`` again."""
    from fpl_intelligence.api.routes.telegram import telegram_webhook

    annotation = telegram_webhook.__annotations__["return"]
    assert annotation is not dict[str, bool]
    assert annotation == dict[str, object] or "Any" in str(annotation)


# --------------------------------------------------------------------------- #
# 2. Credentials must never reach the log stream through httpx URLs
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("logger_name", CREDENTIAL_LEAKING_LOGGERS)
def test_silence_credential_leaking_loggers(logger_name: str) -> None:
    logging.getLogger(logger_name).setLevel(logging.DEBUG)

    silence_credential_leaking_loggers()

    assert logging.getLogger(logger_name).level >= logging.WARNING


def test_httpx_info_records_are_suppressed(caplog: pytest.LogCaptureFixture) -> None:
    """An httpx INFO line (which would carry the bot token) must be dropped."""
    silence_credential_leaking_loggers()
    httpx_logger = logging.getLogger("httpx")

    with caplog.at_level(logging.DEBUG):
        httpx_logger.info("HTTP Request: POST https://api.telegram.org/bot<TOKEN>/getMe")

    assert not [r for r in caplog.records if r.name == "httpx"]
    assert httpx_logger.isEnabledFor(logging.WARNING)


def test_configure_logging_also_silences_them() -> None:
    for name in CREDENTIAL_LEAKING_LOGGERS:
        logging.getLogger(name).setLevel(logging.INFO)

    configure_logging("INFO")

    for name in CREDENTIAL_LEAKING_LOGGERS:
        assert logging.getLogger(name).level >= logging.WARNING


def test_importing_the_api_app_silences_them() -> None:
    """The serverless entrypoint must be safe without any explicit setup call."""
    for name in CREDENTIAL_LEAKING_LOGGERS:
        logging.getLogger(name).setLevel(logging.INFO)

    import importlib

    import fpl_intelligence.api.main as api_main

    importlib.reload(api_main)

    for name in CREDENTIAL_LEAKING_LOGGERS:
        assert logging.getLogger(name).level >= logging.WARNING


def test_building_a_bot_silences_them(monkeypatch: pytest.MonkeyPatch) -> None:
    """Constructing the bot must mute httpx before its client is created."""
    from fpl_intelligence.notifications.telegram_bot import TelegramBot

    for name in CREDENTIAL_LEAKING_LOGGERS:
        logging.getLogger(name).setLevel(logging.INFO)

    TelegramBot("TEST_TOKEN_PLACEHOLDER_NOT_REAL", [4242], dry_run=True)

    for name in CREDENTIAL_LEAKING_LOGGERS:
        assert logging.getLogger(name).level >= logging.WARNING
