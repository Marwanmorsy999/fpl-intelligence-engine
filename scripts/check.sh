#!/usr/bin/env bash
set -euo pipefail
python -m compileall -q src tests
pytest
ruff check .
