# Phase 9.8 — Production Dockerfile for the FPL Intelligence Engine.
#
# Pinned to python:3.12-slim (matches project requires-python >= 3.12), runs as a
# non-root system user, and installs only the runtime dependencies from
# requirements.txt. The application is launched via `python -m fpl_intelligence`.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app

# Create a dedicated non-root user for the running process.
RUN groupadd --system fpl && useradd --system --gid fpl --home /app fpl \
    && chown -R fpl:fpl /app

USER fpl

EXPOSE 8000

ENTRYPOINT ["python", "-m", "fpl_intelligence"]
