# Credentials & Secret Reference

The FPL Intelligence Engine follows a **zero-hardcoded-secrets** policy:
nothing sensitive is committed to the repo (see the `test_deployment_artifacts_contain_no_secrets`
test). Every value below is injected via environment variables / platform
secrets. This is the single inventory for ops.

## Production Vercel checklist

Set these in **Vercel → Project → Settings → Environment Variables** before
going live:

| Priority | Variable | Required | Notes |
|----------|----------|----------|-------|
| 1 | `DATABASE_URL` | Yes | Postgres (Neon / Supabase / your host). Falls back to local SQLite when unset in dev. |
| 2 | `CRON_SECRET` | Yes | Bearer token Vercel Cron + GitHub Actions send as `Authorization: Bearer <CRON_SECRET>`. |
| 3 | `FPL_PROXY_URL` | Recommended | Google Apps Script mirror. Vercel shared egress is often 403/429-blocked by the official FPL API. The egress mask chain tries direct → allorigins → corsproxy.io → this proxy. |
| 4 | `SYNC_PUSH_TOKEN` | No | Machine-to-machine pushes (bookmarklet, Apps Script fetcher, GHA data-refresh). Empty = push endpoints return 503. |
| 5 | `SENTRY_DSN` | No | Backend + frontend error tracking (optional, free tier). |
| 6 | `TELEGRAM_BOT_TOKEN` | No | Enables `/api/v1/telegram/webhook`. |
| 7 | `VAPID_PUBLIC_KEY` / `VAPID_PRIVATE_KEY` / `VAPID_SUBJECT` | No | Browser push notifications (self-hosted web push). |
| 8 | `GROQ_API_KEY` / `GOOGLE_API_KEY` / `OPENROUTER_API_KEY` | No | LLM providers for the AI Analyst. Empty = AI features degrade gracefully. |
| 9 | `THE_ODDS_API_KEY` | No | Market-check enrichment (free tier 500 credits/mo). |
| 10 | `RSS_FEED_URL` | No | Override the BBC RSS source for the News Radar. |

Also set the same secrets in **GitHub Actions → Settings → Secrets and variables → Actions** for any workflows that write to the app.

## 1. Database

| Secret               | Required | Where                         | Notes |
|----------------------|----------|-------------------------------|-------|
| `DATABASE_URL`       | Yes (prod) | Vercel env + GitHub secrets | Postgres (Neon/Supabase). Falls back to local SQLite (`sqlite:///./fpl_local.db`) when unset in non-production — see [docs/DB_SETUP.md]. |

## 2. Scheduled-job auth

| Secret               | Where                         | Notes |
|----------------------|-------------------------------|-------|
| `CRON_SECRET`        | Vercel env (Cron) + GitHub secrets | Bearer token Vercel Cron and the GitHub Actions sync workflow send as `Authorization: Bearer <CRON_SECRET>`. When unset, admin endpoints are open (dev convenience). Set it in prod. |

## 3. Error tracking (Phase 4.4 — fully optional, free 5k events/mo)

| Secret        | Where                              | Required | Notes |
|---------------|------------------------------------|----------|-------|
| `SENTRY_DSN`  | Vercel env + GitHub secrets        | No       | Backend: `main.py` only calls `sentry_sdk.init` when set. Frontend: the dashboard HTML/Sentry browser snippet is injected **only** when set, and every `window.reportError(...)` call-site is a guarded no-op otherwise (zero console noise when absent). Trivy-free: leaving it blank keeps the SDK fully unloaded. |

## 4. Free data providers (optional, all free tiers)

| Secret / var        | Where                | Required | Notes |
|---------------------|----------------------|----------|-------|
| `FPL_PROXY_URL`     | Vercel env           | No       | Google Apps Script mirror used when the FPL API 403s Vercel egress. The egress mask chain tries direct → CORS masks → this proxy. |
| `RSS_FEED_URL`      | Vercel env           | No       | Override the BBC RSS source for the News Radar. |
| `THE_ODDS_API_KEY`  | Vercel env           | No       | The Odds API free tier (500 credits/mo). Absent = market-check enrichment disabled with a warning, never a crash. |

## 5. Push / third-party write auth

| Secret               | Where                | Required | Notes |
|----------------------|----------------------|----------|-------|
| `SYNC_PUSH_TOKEN`    | Vercel env + GitHub secrets | No | Bearer token for machine-to-machine pushes (bookmarklet, Google Apps Script fetcher, GHA data-refresh). When unset, push endpoints answer `503`. |

## 6. Telegram (optional)

| Secret               | Where                | Required | Notes |
|----------------------|----------------------|----------|-------|
| `TELEGRAM_BOT_TOKEN` | Vercel env           | No       | Enables the `/api/v1/telegram/webhook`. Hidden in logs via `silence_credential_leaking_loggers()`. |

## 7. Adding a secret

- **Vercel**: Project → Settings → Environment Variables → import from the
  GitHub secret of the same name.
- **GitHub Actions**: `Settings → Secrets and variables → Actions → New repository secret`.

## 8. Self-check

```bash
# 1. confirm no secrets are committed:
git grep -I -n -E '(Bearer |token|password|api[_-]?key)' -- src/ api/ scripts/
# 2. confirm the local smoke build:
python -m fpl_intelligence prod_migrate && python -m fpl_intelligence
```

If a local dev build starts without `DATABASE_URL`, you should see the SQLite
fallback engage (`db: connected` from `/api/v1/health`) — proving the zero-config
path works before any production secret is wired.
