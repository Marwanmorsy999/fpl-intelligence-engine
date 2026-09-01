#!/usr/bin/env bash
set -euo pipefail

pip install .

if git rev-parse --verify HEAD^ >/dev/null 2>&1 \
  && git diff --quiet HEAD^ HEAD -- migrations alembic.ini src/fpl_intelligence/prod_migrate.py; then
  echo "vercel_build: no migration inputs changed — skipping prod_migrate"
else
  echo "vercel_build: migration inputs changed — running prod_migrate"
  python -m fpl_intelligence.prod_migrate
fi
