#!/usr/bin/env bash
set -euo pipefail

# Vercel build entrypoint lives at repository root because `scripts/**` is
# excluded from the Python function bundle.
pip install .

# Only touch production DB state when migration inputs changed. Ordinary
# application-only deployments do not need a DB connection during build.
BASE_SHA="${VERCEL_GIT_PREVIOUS_SHA:-HEAD^}"

# Vercel checkouts can be shallow; try to fetch the previous commit if Vercel provides it.
if ! git rev-parse --verify "$BASE_SHA" >/dev/null 2>&1 && [ -n "${VERCEL_GIT_PREVIOUS_SHA:-}" ]; then
  git fetch --no-tags --depth=2 origin "${VERCEL_GIT_PREVIOUS_SHA}" >/dev/null 2>&1 || true
fi

if git rev-parse --verify "$BASE_SHA" >/dev/null 2>&1 \
  && git diff --quiet "$BASE_SHA" HEAD -- migrations alembic.ini src/fpl_intelligence/prod_migrate.py; then
  echo "vercel_build: no migration inputs changed — skipping prod_migrate"
else
  echo "vercel_build: migration inputs changed — running prod_migrate"
  python -m fpl_intelligence.prod_migrate
fi
