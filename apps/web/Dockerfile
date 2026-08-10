# syntax=docker/dockerfile:1.7
FROM node:26.7.0-bookworm-slim AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund

FROM node:26.7.0-bookworm-slim AS builder
WORKDIR /app
ARG NEXT_PUBLIC_API_BASE_URL
ARG NEXT_PUBLIC_CHRONOS_RELEASE
ARG NEXT_PUBLIC_CHRONOS_ENVIRONMENT=production
ARG NEXT_PUBLIC_WEB_BASE_URL
ARG NEXT_PUBLIC_TERMS_URL
ARG NEXT_PUBLIC_PRIVACY_URL
ARG NEXT_PUBLIC_SUPPORT_URL
ARG NEXT_PUBLIC_STATUS_URL
ENV NEXT_PUBLIC_API_BASE_URL=$NEXT_PUBLIC_API_BASE_URL
ENV NEXT_PUBLIC_CHRONOS_RELEASE=$NEXT_PUBLIC_CHRONOS_RELEASE \
    NEXT_PUBLIC_CHRONOS_ENVIRONMENT=$NEXT_PUBLIC_CHRONOS_ENVIRONMENT \
    NEXT_PUBLIC_WEB_BASE_URL=$NEXT_PUBLIC_WEB_BASE_URL \
    NEXT_PUBLIC_TERMS_URL=$NEXT_PUBLIC_TERMS_URL \
    NEXT_PUBLIC_PRIVACY_URL=$NEXT_PUBLIC_PRIVACY_URL \
    NEXT_PUBLIC_SUPPORT_URL=$NEXT_PUBLIC_SUPPORT_URL \
    NEXT_PUBLIC_STATUS_URL=$NEXT_PUBLIC_STATUS_URL
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY . .
RUN mkdir -p public
ENV NEXT_TELEMETRY_DISABLED=1
# The browser DSN is public by protocol but is still carried as a BuildKit
# secret so it never appears in build arguments, image history, or CI logs.
RUN --mount=type=secret,id=sentry_dsn \
    if [ -f /run/secrets/sentry_dsn ]; then \
      export NEXT_PUBLIC_SENTRY_DSN="$(cat /run/secrets/sentry_dsn)"; \
      export SENTRY_DSN="$NEXT_PUBLIC_SENTRY_DSN"; \
    fi; \
    npm run build

FROM node:26.7.0-bookworm-slim AS runner
WORKDIR /app
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0

# Carry over only what's needed to run.
COPY --chown=node:node --from=deps /app/node_modules ./node_modules
COPY --chown=node:node --from=builder /app/.next ./.next
COPY --chown=node:node --from=builder /app/public ./public
COPY --chown=node:node --from=builder /app/package.json ./package.json

USER node

EXPOSE 3000
CMD ["node_modules/.bin/next", "start", "--port", "3000"]
