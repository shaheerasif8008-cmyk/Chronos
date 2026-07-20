# syntax=docker/dockerfile:1.7
FROM python:3.12.10-slim-bookworm AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH

WORKDIR /app

# Build-only compilers never cross into the runtime image.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev gcc g++ libffi-dev libssl-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements.lock ./
RUN python -m venv "$VIRTUAL_ENV" \
    && pip install --upgrade "pip==26.1.2" \
    && pip install --require-hashes -r requirements.lock


FROM python:3.12.10-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv

# Keep only runtime libraries and the health probe in the final layer.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 curl \
    && rm -rf /var/lib/apt/lists/*

# The production browser operator connects to Browserbase over CDP and does not
# run Chromium in the API container. This avoids both process-local sessions and
# disabling the Chromium sandbox in a container. Local debug images can opt
# in to a sandboxed Chromium binary explicitly.
ARG INSTALL_PLAYWRIGHT=0
RUN if [ "$INSTALL_PLAYWRIGHT" = "1" ]; then \
        playwright install chromium --with-deps; \
    fi \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --system --gid 10001 chronos \
    && useradd --system --uid 10001 --gid chronos --create-home chronos \
    && mkdir -p /ms-playwright /tmp/chronos_computers /tmp/chronos_workspaces \
    && chown -R chronos:chronos /ms-playwright /tmp/chronos_computers /tmp/chronos_workspaces

COPY --chown=chronos:chronos . .

USER chronos

EXPOSE 8000

# Migrations run via a separate ECS task (see deploy workflow); the server
# assumes the schema is already up to date when it starts.
# SSE connections are intentionally long-lived. Bound Uvicorn's connection
# drain so a rollout cannot wait forever on an open browser stream; ECS grants
# a larger stop window for lifespan cleanup after this deadline.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2", "--timeout-graceful-shutdown", "45"]
