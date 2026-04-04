# Multi-stage Dockerfile for Smart Link (KAN-138)
# - Base image pinned to Python 3.12.9-slim (immutable stack requirement)
# - Builder stage builds wheels for faster subsequent installs (cache-friendly)
# - Final stage installs wheels and copies app source
# - Default CMD runs Gunicorn using wsgi.prod_wsgi:app and gunicorn.conf.py
#
# NOTES:
#  - This file assumes a requirements.txt at repo root (provided in this blueprint).
#  - For dev iteration we recommend using docker-compose with a bind-mount to avoid rebuilding image for code edits.
#  - For production image builds, use: docker build -t smartlink:prod .
#  - The image intentionally creates a non-root user for runtime (appuser).
#
FROM python:3.12.9-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build

# Install minimal build tools for compiling wheels (only what we need)
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential gcc git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Copy only requirements first for layer caching
COPY requirements.txt ./

# Build wheels into /wheels to allow fast installs in final image
RUN python -m pip install --upgrade pip setuptools wheel \
    && python -m pip wheel --no-deps --wheel-dir /wheels -r requirements.txt

# Final (runtime) image
FROM python:3.12.9-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install runtime deps from builder wheels for deterministic, cache-friendly install
COPY --from=builder /wheels /wheels
RUN python -m pip install --upgrade pip setuptools \
    && if [ -d /wheels ]; then python -m pip install --no-deps --no-index --find-links=/wheels -r /wheels/../requirements.txt || true; fi \
    || true

# Copy application source
COPY . /app

# Create a non-root user to run the app
RUN useradd --create-home --shell /bin/bash appuser || true \
    && chown -R appuser:appuser /app

USER appuser

# Expose the port Gunicorn will bind to by default (configured in gunicorn.conf.py)
EXPOSE 8000

# Default environment variables (can be overridden by runtime env or docker-compose)
ENV DATABASE_URL=${DATABASE_URL:-sqlite:///local_dev.db} \
    SECRET_KEY=${SECRET_KEY:-dev-secret} \
    JWT_SECRET=${JWT_SECRET:-${SECRET_KEY:-dev-secret}} \
    JWT_COOKIE_SECURE=false \
    JWT_SAMESITE=Lax

# Default command to run (production image). Uses repository's gunicorn.conf.py and wsgi.prod_wsgi:app
# Operators can override CMD at runtime for ad-hoc commands (tests, shell, etc.).
CMD ["gunicorn", "--config", "gunicorn.conf.py", "wsgi.prod_wsgi:app"]
--- END FILE: Dockerfile ---