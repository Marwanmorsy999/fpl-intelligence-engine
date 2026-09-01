#!/usr/bin/env bash
set -euo pipefail

pip install .

# Database migrations are needed when migration inputs change. For ordinary
# application-only commits, avoid opening a production DB connection during
# every Vercel build; repeated build-triggered migration attempts were causing
# connection/statement churn without changing the schema.
if git rev-parse --verify HEAD^ >/dev/null 2>&1 \
  && git diff --quiet HEAD^ HEAD -- migrations alembic.ini src/fpl_intelligence/prod_migrate.py; then
  echo "vercel_build: no migration inputs changed — skipping prod_migrate"
else
  echo "vercel_build: migration inputs changed — running prod_migrate"
  python -m fpl_intelligence.prod_migrate
fi
