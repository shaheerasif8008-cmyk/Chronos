# syntax=docker/dockerfile:1.7
FROM python:3.11.9-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# System deps: libpq for asyncpg, gcc for compiled wheels, and curl for health
# checks. Playwright installs its own browser OS dependencies below.
# Xvfb/xdotool/scrot back the desktop GUI operator (connectors/desktop.py); the
# bridge degrades truthfully if they are absent, but the production image ships
# them so desktop tasks run for real.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev gcc g++ libffi-dev libssl-dev curl \
    xvfb xdotool scrot x11-utils \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps in a separate layer so code changes don't bust the cache.
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# Playwright chromium (browser connector). The production API image must include
# Chromium so broker-routed browser tasks do not start in degraded mode. Local
# debug builds can opt out with INSTALL_PLAYWRIGHT=0.
ARG INSTALL_PLAYWRIGHT=1
RUN if [ "$INSTALL_PLAYWRIGHT" = "1" ]; then \
        playwright install chromium --with-deps; \
    fi

COPY . .

EXPOSE 8000

# Migrations run via a separate ECS task (see deploy workflow); the server
# assumes the schema is already up to date when it starts.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
