#!/usr/bin/env bash
set -euo pipefail

# Vercel builds must be hermetic. Production database migrations are an
# explicit operations step, never a build prerequisite; this prevents a
# transient pool/auth outage from turning a valid application build into a
# failed deployment.
pip install .

echo "vercel_build: application build complete; production migrations are managed separately"
