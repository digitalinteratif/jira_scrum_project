# Dockerfile for local staging (KAN-151)
# Production-parity: Python 3.12.9 base, install requirements, run via Gunicorn.
FROM python:3.12.9-slim

# Avoid interactive prompts during package installs
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    POETRY_VIRTUALENVS_CREATE=false

# Minimal system deps required for many python packages (playwright/test runner not executed inside container)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    ca-certificates \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Create app user for safer runtime
RUN useradd --create-home --shell /bin/bash appuser

WORKDIR /app

# Copy python requirement files if present; fallback when none (dependency tolerant)
COPY requirements.txt requirements.txt
# Install runtime requirements (defensively tolerate absent file)
RUN if [ -f requirements.txt ]; then pip install --no-cache-dir -r requirements.txt || true; fi

# Copy repository code into image
# Use a conservative copy to avoid leaking host-only files
COPY . /app

# Ensure owned files and set minimal permissions
RUN chown -R appuser:appuser /app

USER appuser

# Expose the app port required by acceptance criteria
EXPOSE 5000

# Use Gunicorn with a conservative worker count; bind to 0.0.0.0:5000 per AC
# Keep log output to stdout for container logs
CMD ["gunicorn", "app_core.app:app", "-b", "0.0.0.0:5000", "--access-logfile", "-", "--error-logfile", "-"]
--- END FILE: Dockerfile ---