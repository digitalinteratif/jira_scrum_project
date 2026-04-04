# Dockerfile for local staging (Production-parity: Python 3.12.9 + Gunicorn)
FROM python:3.12.9-slim

# Install OS deps required for typical Python packages (build-essential optional)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential curl ca-certificates gcc libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Create app user and workdir
ENV APP_HOME=/app
WORKDIR ${APP_HOME}

# Copy project files (assumes repository root contains requirements.txt and app_core/)
# Copying only required files to keep image small; docker-compose will mount sources during development if needed.
COPY requirements.txt ./

# Install Python deps
RUN python -m pip install --upgrade pip setuptools wheel \
    && pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Expose port used by app (gunicorn will bind to 0.0.0.0:5000 per acceptance criteria)
EXPOSE 5000

# Use a non-root user for safety when possible
RUN useradd --create-home --shell /bin/bash appuser || true
USER appuser

# Default envs (can be overridden via docker-compose or environment)
ENV DATABASE_URL="sqlite:///shortener.db"
ENV BASE_URL="http://localhost:5000"
ENV PORT=5000
ENV PYTHONUNBUFFERED=1

# Healthcheck used by Docker runtime (also leveraged by orchestrator script)
HEALTHCHECK --interval=5s --timeout=3s --start-period=5s --retries=6 \
  CMD curl -fsS http://localhost:5000/health || exit 1

# Entrypoint: use gunicorn to boot the modular Flask app (app_core.app:app) on port 5000
CMD ["gunicorn", "app_core.app:app", "-b", "0.0.0.0:5000", "--log-level", "info"]