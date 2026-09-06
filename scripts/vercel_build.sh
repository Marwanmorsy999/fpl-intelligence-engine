#!/usr/bin/env bash
set -euo pipefail

# Vercel builds must be hermetic: never require the production database to
# build or import application code. Database migrations are a separate,
# explicitly controlled deployment/operations step.
pip install .

echo "vercel_build: application build complete; production migrations are managed separately"
