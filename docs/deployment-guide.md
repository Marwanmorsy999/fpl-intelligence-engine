# Deployment Guide — FPL Intelligence Engine (Phase 11.2)

This guide covers deploying the FPL Intelligence Engine to a PaaS (Railway or
Render) or a VPS using Docker Compose. The application runs as **four process
types** defined in the root `Procfile`:

| Process  | Command                                                | Purpose                                  |
| -------- | ------------------------------------------------------ | ---------------------------------------- |
| `web`    | `uvicorn fpl_intelligence.api.main:app`                | FastAPI JSON API + (optional) dashboard |
| `worker` | `python -m fpl_intelligence.scripts.run_scheduler`     | Phase 9.6 live-source scheduler          |
| `bot`    | `python -m fpl_intelligence.scripts.run_telegram_bot`  | Phase 10.2 Telegram bot                  |
| `release`| `alembic upgrade head`                                 | Apply DB migrations on every deploy      |

> **Never commit secrets.** Everything sensitive is supplied through platform
> environment variables. Copy `.env.example` to `.env` locally and use the
> platform's secret/env UI in production.

---

## 1. Prerequisites

- A PostgreSQL database (provisioned by the platform or your VPS).
- Python 3.12+ (the image builds with Python 3.12).
- The following environment variables set (see `.env.example`):
  - `DATABASE_URL` — e.g. `postgresql+psycopg://user:pass@host:5432/db`
  - `APP_ENV=production`
  - Optional data/LLM keys: `API_FOOTBALL_KEY`, `FOOTBALL_DATA_ORG_KEY`,
    `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USER_IDS`, `GROQ_API_KEY`,
    `GOOGLE_API_KEY`, `OPENROUTER_API_KEY`.
  - `FPL_API_USE_LIVE_LLM=false` (safe default — no live LLM calls).
  - `SERVE_STATIC_DASHBOARD=true` (or `false` to run the API only).
  - `CORS_ORIGINS` — comma-separated origins if a separate frontend calls the API.

---

## 2. Deploy to Railway

1. **Create a new project** from the Railway dashboard and choose
   *Deploy from GitHub repo*. Connect the repository
   (`fpl-intelligence-engine-foundation`).
2. **Add a PostgreSQL plugin** (Databases → Postgres). Railway auto-injects
   `DATABASE_URL`; copy its value into your service's environment variables
   but rewrite the scheme to `postgresql+psycopg://` (Railway supplies the
   `postgres://` scheme which SQLAlchemy 2.0 does not accept).
3. **Set the service as a Web Service** using the repo's `Procfile`. Railway
   auto-detects the `web` process. Set:
   - *Start Command*: `web` (or leave blank — Railway uses the Procfile).
   - *Healthcheck Path*: `/health`.
4. **Add the environment variables** from `.env.example` (Railway → Variables).
   Generate a strong `POSTGRES_PASSWORD` if you use `docker-compose` style vars.
5. **Provision the worker and bot** as separate Railway services from the same
   repo, each started with the `worker` and `bot` Procfile commands
   respectively. They share the same `DATABASE_URL` and env vars.
6. **Deploy.** Railway runs the `release` command (`alembic upgrade head`)
   automatically before the app starts. Watch the deploy logs to confirm
   `INFO  [alembic] ...` migration output and `Application startup complete`.
7. **Verify:** open `https://<your-service>.up.railway.app/health` — it should
   return `{"status":"ok","version":"..."}`.

---

## 3. Deploy to Render

1. **New → Web Service**, connect the GitHub repo.
2. **Environment:** `Python 3`.
3. **Build Command:** `pip install -e ".[dev]"` (or your project's install
   command). **Start Command:** `uvicorn fpl_intelligence.api.main:app
   --host 0.0.0.0 --port $PORT`.
4. **Add a PostgreSQL instance** (Render → New → PostgreSQL). Copy the
   *Internal Database URL* and set `DATABASE_URL` to the
   `postgresql+psycopg://...` form.
5. **Set environment variables** from `.env.example` under *Environment*.
6. **Run migrations:** under *Settings → Deploy Hook* (or a one-off shell),
   run `alembic upgrade head`. Render also supports a `release` phase if you
   keep the Procfile — set the *Pre-deploy command* to `alembic upgrade head`.
7. **Add the worker and bot** as separate *Background Worker* services from the
   same repo with the start commands `python -m
   fpl_intelligence.scripts.run_scheduler` and `python -m
   fpl_intelligence.scripts.run_telegram_bot`.
8. **Verify:** `https://<your-service>.onrender.com/health`.

---

## 4. Deploy on a VPS with Docker Compose

This simulates the full production stack (FastAPI + Postgres + Worker + Bot)
using `docker-compose.prod.yml`.

```bash
# 1. Create your secrets file (git-ignored) from the template.
cp .env.example .env
#   Edit .env and set at least POSTGRES_PASSWORD and DATABASE_URL.

# 2. Build and start everything.
docker compose -f docker-compose.prod.yml up -d --build

# 3. Confirm migrations ran (the `release` step runs on `web` start via the
#    `app` image; verify with):
docker compose -f docker-compose.prod.yml exec app alembic upgrade head

# 4. Health check.
curl http://localhost:8000/health
```

The `worker` and `bot` containers run the scheduler and Telegram bot. Set
`TELEGRAM_BOT_TOKEN` / `TELEGRAM_ALLOWED_USER_IDS` for the bot to be active;
otherwise it logs a configuration error and exits.

---

## 5. Frontend separation (Vercel / Netlify)

By default the FastAPI app also serves the static dashboard SPA. To host the
frontend separately (e.g. on Vercel):

1. In the API deployment set `SERVE_STATIC_DASHBOARD=false` so the app is a pure
   JSON API.
2. Set `CORS_ORIGINS=https://your-frontend.vercel.app` (comma-separated for
   multiple origins).
3. Extract `src/fpl_intelligence/web/static/dashboard.html` (and its assets) to
   your static host, and point its API base URL at the deployed API.
4. The dashboard calls the same `/api/v1/*` endpoints documented in
   `docs/phase10-web-dashboard.md`.

---

## 6. 100% Free (No Credit Card) — Vercel + GitHub Actions + Supabase

This tier uses **only free, no-credit-card-required** services:

| Service      | Free tier used (no card)                              |
| ------------ | ---------------------------------------------------- |
| **Supabase** | Free Postgres (500 MB DB, no card).                 |
| **Vercel**  | Hobby tier (serverless functions, no card).         |
| **GitHub Actions** | Free cron scheduler (public repos; 2,000 min/mo private). |

Vercel's hobby tier **cannot run long-lived background workers**, so the
Phase 9.6 scheduler and the Telegram bot are driven by **GitHub Actions cron**
(` .github/workflows/scheduler.yml`) instead of a Procfile `worker`/`bot`
process. The FastAPI app is served by a single Vercel serverless function
configured via `vercel.json` (Vercel does **not** use a `Procfile`).

### 6.1 Deploy the API to Vercel

1. Push the repo to GitHub, then **Import** it into Vercel (no framework
   preset needed; Vercel reads `vercel.json`).
2. Under **Settings → Build & Development**, confirm the build command is
   `pip install -r requirements.txt` (set in `vercel.json`).
3. Add the environment variables from §6.3 below.
4. **Deploy.** Vercel builds the FastAPI app from
   `src/fpl_intelligence/api/main.py` and rewrites every request — including
   `/api/v1/telegram/webhook`, `/api/v1/admin/run-scheduler`, and
   `/health` — to that serverless function. The `data/` and `tests/`
   directories are excluded from the function bundle to keep it small.
5. **Verify:** `https://<your-app>.vercel.app/health` returns
   `{"status":"ok","version":"..."}`.

### 6.2 Wire up the scheduler (GitHub Actions)

The `scheduler.yml` workflow runs on two cron schedules:

- `0 * * * *` — hourly: `POST /api/v1/admin/run-scheduler` (authenticated with
  the `X-Admin-Secret` header).
- `*/10 * * * *` — every 10 minutes: `GET /api/v1/health` to keep the
  serverless function warm (avoids Vercel's cold-start latency).

Go to ** repo → Settings → Secrets and variables → Actions** and add the two
secrets from §6.3. The workflow reads them as `secrets.VERCEL_DEPLOY_URL` and
`secrets.ADMIN_SECRET_KEY`. You can also trigger it manually via
**Actions → Run workflow**.

### 6.3 Required environment variables

**Vercel — Settings → Environment Variables:**

| Variable                     | Value / example                                                  |
| ---------------------------- | --------------------------------------------------------------- |
| `DATABASE_URL`               | Supabase Postgres, `postgresql+psycopg://...` form              |
| `APP_ENV`                    | `production`                                                    |
| `ADMIN_SECRET_KEY`           | a strong random string (must match GitHub `ADMIN_SECRET_KEY`)  |
| `FPL_API_USE_LIVE_LLM`       | `false` (safe default — no live LLM calls)                     |
| `SERVE_STATIC_DASHBOARD`     | `false` (Vercel hosts the API only)                            |
| `CORS_ORIGINS`               | comma-separated frontend origins, or empty                    |
| `API_FOOTBALL_KEY`           | *(optional)*                                                    |
| `FOOTBALL_DATA_ORG_KEY`      | *(optional)*                                                    |
| `TELEGRAM_BOT_TOKEN`         | *(optional)* Phase 10.2 bot token                              |
| `TELEGRAM_ALLOWED_USER_IDS`  | *(optional)* comma-separated Telegram user IDs                |
| `GROQ_API_KEY` / `GOOGLE_API_KEY` / `OPENROUTER_API_KEY` | *(optional)* LLM keys |

**GitHub — Settings → Secrets → Actions:**

| Secret                | Value                                                        |
| --------------------- | ----------------------------------------------------------- |
| `VERCEL_DEPLOY_URL`   | `https://<your-app>.vercel.app` (no trailing slash)         |
| `ADMIN_SECRET_KEY`    | **identical** value to Vercel's `ADMIN_SECRET_KEY`          |

> The `/api/v1/admin/run-scheduler` endpoint only accepts requests whose
> `X-Admin-Secret` header equals Vercel's `ADMIN_SECRET_KEY`. Because GitHub
> sends `secrets.ADMIN_SECRET_KEY`, the two values **must be the same**.

---

## 7. Post-deploy checklist

- [ ] `GET /health` returns `ok`.
- [ ] `alembic current` shows `0013_squad_state_persistence` (head).
- [ ] `POST /api/v1/squad` then `GET /api/v1/squad` round-trips after a restart
      (proves PostgreSQL persistence).
- [ ] `worker` and `bot` processes are running (or intentionally disabled).
- [ ] No real API keys are committed; `FPL_API_USE_LIVE_LLM=false`.
- [ ] `.env` / platform secrets are not in version control.
