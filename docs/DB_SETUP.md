# Database Setup (free tier)

The FPL Intelligence Engine is architected to run against a **single Postgres
database** in production, with a zero-config **SQLite fallback for local dev**.

## TL;DR

| Tier | Provider | Cost | Purpose |
|------|----------|------|---------|
| Production | Neon (Postgres) or Supabase (Postgres) | Free | shared by Vercel + GitHub Actions |
| Local dev | SQLite `./fpl_local.db` | Free | runs with `python -m fpl_intelligence` — no config needed |

## 1. Production — Neon (recommended) or Supabase

Both offer a generous free Postgres tier with connection pooling that works from
Vercel's shared egress (port 5432 for Neon; Supabase exposes a pooler on 6543).

### Neon

1. Sign up at <https://neon.tech> (free tier).
2. Create a project — a default database `neondb` and role `neondb_owner` are
   created automatically.
3. Copy the connection string from the Neon console, e.g.
   ```
   postgresql://<role>:<pass>@<branch>-<random>.postgres.database.azure.com:5432/neondb?sslmode=require
   ```
4. Set it as the `DATABASE_URL` secret on **Vercel** and in the repo
   **GitHub Actions secrets** (`Settings → Secrets and variables → Actions`).
   See [../SETUP_CREDENTIALS.md] for the exact secret name.

### Supabase

1. Sign up at <https://supabase.com> (free tier).
2. Create a project; the database is ready in ~1 minute.
3. Connection string lives under **Project Settings → Database → Connection
   string** (URI). Use the `5432` direct port (not the 6543 pooler) unless you
   hit pooling issues.
4. Set as the `DATABASE_URL` secret on Vercel / GitHub Actions.

> **Pooling note:** Supabase exposes PostgreSQL through a transaction-mode
> pooler (port 6543). `psycopg3` pre-prepares repeated statements which
> collides on a pooled connection (`prepared statement already exists`).
> `src/fpl_intelligence/db/session.py` detects `postgres://` URLs and passes
> `prepare_threshold=None` to disable prepared statements automatically — so the
> pooler just works. SQLite URLs (local dev) are unaffected.

## 2. Local dev — SQLite (no config)

If `DATABASE_URL` is unset (or still carries its built-in placeholder) **and**
the app is not in production mode, `db/session.py` transparently falls back to
SQLite:

```python
sqlite:///./fpl_local.db
```

That means:

```bash
python -m fpl_intelligence        # dev server on :8000, backed by SQLite
python -m fpl_intelligence.prod_migrate   # run migrations against the SQLite db
```

No Docker, no Postgres install, no network — the whole stack boots locally.

## 3. Migrations

Migrations are Alembic and live in `migrations/`. The Vercel build command runs
them for you:

```jsonc
// vercel.json
"buildCommand": "pip install . && python -m fpl_intelligence.prod_migrate"
```

Locally (SQLite), run the same command. For Neon/Supabase, run it against
`DATABASE_URL`:

```bash
DATABASE_URL=postgresql://... python -m fpl_intelligence.prod_migrate
```

## 4. Health check

`/api/v1/health` returns `{"status": "ok"|"degraded", "db": "<url>|<error>",
"version": "<semver>"}`. A `200`/`ok` means the engine is talking to the
database; `degraded` with a `postgres` error means the `DATABASE_URL` secret is
missing or mis-typed. The endpoint never crashes — it always returns JSON.

## 5. Troubleshooting

- **`prepared statement already exists`** → using the Supabase pooler; the app
  auto-disables prepared statements, so this points to a very old `psycopg` —
  upgrade to `psycopg[binary]>=3.2`.
- **SQLite lock errors under concurrent requests** → the free tier is single-writer;
  run a real Postgres locally via `pip install pgvector` + docker if you need
  concurrency, or just use the file serially.
- **`relation does not exist`** → migrations haven't run; execute
  `python -m fpl_intelligence.prod_migrate`.
