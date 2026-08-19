# Phase 11.2 — PaaS process types (Render / Railway / Heroku).
#
# Secrets flow from the platform environment variables; nothing sensitive is
# hardcoded. `release` runs Alembic migrations automatically on every deploy.

web: uvicorn fpl_intelligence.api.main:app --host 0.0.0.0 --port ${PORT:-8000}
worker: python -m fpl_intelligence.scripts.run_scheduler
bot: python -m fpl_intelligence.scripts.run_telegram_bot
release: alembic upgrade head
