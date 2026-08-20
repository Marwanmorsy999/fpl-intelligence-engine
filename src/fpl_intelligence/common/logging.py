import logging

#: Third-party loggers that echo full request URLs at INFO/DEBUG level. Those
#: URLs embed credentials: ``python-telegram-bot`` drives the Telegram API over
#: httpx, so an INFO record reads
#: ``HTTP Request: POST https://api.telegram.org/bot<TOKEN>/getMe "200 OK"``
#: and the bot token lands verbatim in the platform log stream (Vercel, Docker,
#: journald, ...). Provider clients can leak keyed query strings the same way.
CREDENTIAL_LEAKING_LOGGERS = ("httpx", "httpcore")


def silence_credential_leaking_loggers(level: int = logging.WARNING) -> None:
    """Raise the level of third-party loggers that print secrets inside URLs.

    Idempotent, and safe to call before or after :func:`configure_logging`.
    """
    for name in CREDENTIAL_LEAKING_LOGGERS:
        logging.getLogger(name).setLevel(level)


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    silence_credential_leaking_loggers()
