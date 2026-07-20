"""Authenticated, secret-free runtime readiness reporting.

The public liveness/readiness probes intentionally stay small and anonymous.
This module builds the richer operator view used by tenant settings and first-run
setup without returning credentials, provider response bodies, worker ids, or
other deployment secrets.
"""
from __future__ import annotations

from datetime import datetime, timezone
import time
from typing import Any

from sqlalchemy import select

from core import authz, billing, notification_delivery
from core.config import settings
from core.connector_health import check_connectors
from core.db import engine
from core.file_security import scanner_health
from core.object_storage import check_bucket
from core.redis import redis_client


_HEALTHY_PROVIDER_STATES = {"live", "verified", "available"}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat().replace("+00:00", "Z") if value else None


def _check(
    check_id: str,
    label: str,
    *,
    required: bool,
    status: str,
    summary: str,
    remediation: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": check_id,
        "label": label,
        "required": required,
        "status": status,
        "summary": summary,
    }
    if remediation:
        result["remediation"] = remediation
    if metadata:
        result["metadata"] = metadata
    return result


def _provider_status(entry: dict[str, Any]) -> str:
    status = str(entry.get("status") or "unavailable")
    tier = str(entry.get("tier") or "unavailable")
    if status in _HEALTHY_PROVIDER_STATES or tier == "live":
        return "healthy"
    if entry.get("configured"):
        return "degraded"
    return "unavailable"


def _provider_check(
    check_id: str,
    label: str,
    entry: dict[str, Any],
    *,
    required: bool = False,
) -> dict[str, Any]:
    status = _provider_status(entry)
    reason = str(entry.get("reason") or f"{label} has not reported readiness.")
    setup = entry.get("setup")
    metadata = {
        "configured": bool(entry.get("configured")),
        "verified": bool(entry.get("verified")),
        "stale": bool(entry.get("stale", True)),
        "checked_at": entry.get("checked_at"),
        "verified_at": entry.get("verified_at"),
        "latency_ms": entry.get("latency_ms"),
        "error_code": entry.get("error_code"),
    }
    return _check(
        check_id,
        label,
        required=required,
        status=status,
        summary=reason,
        remediation=str(setup) if setup else None,
        metadata=metadata,
    )


async def _worker_health(*, required: bool) -> dict[str, Any]:
    from connectors.worker_main import (
        WORKER_HEARTBEAT_PREFIX,
        WORKER_HEARTBEAT_TTL_SECONDS,
    )

    newest: float | None = None
    count = 0
    try:
        async for key in redis_client.scan_iter(
            match=f"{WORKER_HEARTBEAT_PREFIX}*", count=100
        ):
            raw = await redis_client.get(key)
            try:
                heartbeat = float(raw.decode() if isinstance(raw, bytes) else raw)
            except (TypeError, ValueError):
                continue
            count += 1
            newest = heartbeat if newest is None else max(newest, heartbeat)
    except Exception:  # noqa: BLE001 - health output must stay redacted
        return _check(
            "connector_worker",
            "Connector worker",
            required=required,
            status="unavailable",
            summary="Worker heartbeat state could not be read.",
            remediation="Check the connector-worker service and its Redis access.",
            metadata={"active_replicas": 0, "last_seen_at": None},
        )

    age_seconds = max(0, int(time.time() - newest)) if newest is not None else None
    healthy = newest is not None and age_seconds is not None and age_seconds <= WORKER_HEARTBEAT_TTL_SECONDS
    return _check(
        "connector_worker",
        "Connector worker",
        required=required,
        status="healthy" if healthy else "unavailable",
        summary=(
            f"{count} worker replica{'s are' if count != 1 else ' is'} publishing a current heartbeat."
            if healthy
            else "No current connector-worker heartbeat was found."
        ),
        remediation=(
            None
            if healthy
            else "Start or restart the connector-worker service and verify Redis connectivity."
        ),
        metadata={
            "active_replicas": count,
            "last_seen_at": (
                _iso(datetime.fromtimestamp(newest, tz=timezone.utc))
                if newest is not None
                else None
            ),
            "age_seconds": age_seconds,
            "heartbeat_ttl_seconds": WORKER_HEARTBEAT_TTL_SECONDS,
        },
    )


async def build_runtime_health_report(
    *,
    can_admin: bool,
    refresh_providers: bool = False,
) -> dict[str, Any]:
    """Return platform readiness without exposing deployment or tenant secrets."""

    required_checks: list[dict[str, Any]] = []
    optional_checks: list[dict[str, Any]] = []

    try:
        async with engine.begin() as conn:
            await conn.execute(select(1))
        database = _check(
            "database",
            "Database",
            required=True,
            status="healthy",
            summary="The primary database accepted a bounded query.",
        )
    except Exception:  # noqa: BLE001 - never return connection details
        database = _check(
            "database",
            "Database",
            required=True,
            status="unavailable",
            summary="The primary database did not accept the readiness query.",
            remediation="Check database availability, TLS, credentials, and service-network access.",
        )
    required_checks.append(database)

    try:
        await redis_client.ping()
        redis = _check(
            "redis",
            "Coordination store",
            required=True,
            status="healthy",
            summary="Redis accepted the readiness ping.",
        )
    except Exception:  # noqa: BLE001
        redis = _check(
            "redis",
            "Coordination store",
            required=True,
            status="unavailable",
            summary="Redis did not accept the readiness ping.",
            remediation="Check Redis availability, TLS, credentials, and service-network access.",
        )
    required_checks.append(redis)

    try:
        await check_bucket()
        storage = _check(
            "object_storage",
            "Object storage",
            required=True,
            status="healthy",
            summary="The configured object-storage bucket is reachable.",
        )
    except Exception:  # noqa: BLE001
        storage = _check(
            "object_storage",
            "Object storage",
            required=True,
            status="unavailable",
            summary="The configured object-storage bucket is not reachable.",
            remediation="Check the bucket, region, task role, and object-storage network path.",
        )
    required_checks.append(storage)

    scanner_required = settings.is_production or settings.malware_scan_required
    scanner_state = await scanner_health()
    scanner = _check(
        "file_security",
        "File security scanner",
        required=scanner_required,
        status="healthy" if scanner_state["healthy"] else "unavailable",
        summary=(
            "ClamAV is serving malware verdicts for file ingress."
            if scanner_state["healthy"]
            else "The malware scanner is not serving authoritative verdicts."
        ),
        remediation=(
            None
            if scanner_state["healthy"]
            else "Start the private ClamAV sidecar and verify its signature database and local TCP health."
        ),
        metadata={
            "engine": scanner_state["engine"],
            "version": scanner_state["version"],
            "fail_closed": scanner_required,
        },
    )
    (required_checks if scanner_required else optional_checks).append(scanner)

    authorization_required = settings.is_production
    if authz.is_enabled():
        try:
            authorization_ok = await authz.healthcheck()
        except Exception:  # noqa: BLE001
            authorization_ok = False
        authorization = _check(
            "authorization",
            "Authorization service",
            required=authorization_required,
            status="healthy" if authorization_ok else "unavailable",
            summary=(
                "OpenFGA and its datastore are serving."
                if authorization_ok
                else "OpenFGA did not report a serving datastore."
            ),
            remediation=(
                None
                if authorization_ok
                else "Check the OpenFGA service, datastore, API token, and network path."
            ),
        )
    else:
        authorization = _check(
            "authorization",
            "Authorization service",
            required=authorization_required,
            status="unavailable",
            summary=(
                "OpenFGA is not configured; development uses deterministic role gates."
                if not authorization_required
                else "OpenFGA is not configured."
            ),
            remediation="Configure the OpenFGA URL and API token, then run bootstrap.",
        )
    (required_checks if authorization_required else optional_checks).append(authorization)

    worker = await _worker_health(required=settings.is_production)
    (required_checks if settings.is_production else optional_checks).append(worker)

    try:
        provider_health = await check_connectors(refresh=refresh_providers)
    except Exception:  # noqa: BLE001
        provider_health = {}

    primary_model = _provider_check(
        "primary_model",
        "Primary model provider",
        dict(provider_health.get("openrouter") or {}),
        required=settings.is_production,
    )
    if not provider_health.get("openrouter"):
        primary_model.update(
            status="unavailable",
            summary="The primary model provider could not be verified.",
            remediation="Configure and verify the provider used by the primary and fast models.",
        )
    (required_checks if settings.is_production else optional_checks).append(primary_model)

    identity_ok = (
        settings.auth_provider == "cognito"
        and bool(settings.cognito_user_pool_id.strip())
        and bool(settings.cognito_app_client_id.strip())
        and bool(settings.cognito_domain.strip())
    ) if settings.is_production else settings.auth_provider in {"dev_otp", "both", "cognito"}
    identity = _check(
        "identity",
        "Identity provider",
        required=True,
        status="healthy" if identity_ok else "unavailable",
        summary=(
            "The production identity-provider configuration is present."
            if settings.is_production and identity_ok
            else "The development identity mode is available."
            if identity_ok
            else "The required identity-provider configuration is incomplete."
        ),
        remediation=(
            None
            if identity_ok
            else "Configure the Cognito user pool, app client, hosted domain, and HTTPS callback."
        ),
    )
    required_checks.append(identity)

    optional_provider_specs = (
        ("isolated_execution", "Isolated code and data runtime", "e2b"),
        ("cloud_computer", "Cloud computer runtime", "computer"),
        ("browser_operator", "Browser operator", "browser_operator"),
        ("repository_runtime", "Repository runtime", "repo"),
        ("managed_connectors", "Managed SaaS connectors", "composio"),
    )
    for check_id, label, provider_key in optional_provider_specs:
        optional_checks.append(
            _provider_check(
                check_id,
                label,
                dict(provider_health.get(provider_key) or {}),
            )
        )

    email_ok = notification_delivery.email_is_configured()
    optional_checks.append(
        _check(
            "email_delivery",
            "Email delivery",
            required=False,
            status="healthy" if email_ok else "unavailable",
            summary=(
                "Transactional email delivery is configured."
                if email_ok
                else "Email delivery is not configured; invitations use secure manual links."
            ),
            remediation=(
                None
                if email_ok
                else "Configure the email provider and a verified notification sender."
            ),
        )
    )

    billing_ok = billing.is_configured()
    optional_checks.append(
        _check(
            "billing",
            "Billing",
            required=False,
            status="healthy" if billing_ok else "unavailable",
            summary=(
                "Subscription checkout, webhooks, and the billing portal are configured."
                if billing_ok
                else "Billing is not configured; subscription management is unavailable."
            ),
            remediation=(
                None
                if billing_ok
                else "Configure the billing secret, webhook secret, and distinct plan prices."
            ),
        )
    )

    tracing_ok = bool(
        settings.langfuse_public_key.strip() and settings.langfuse_secret_key.strip()
    )
    errors_ok = bool(settings.sentry_dsn.strip())
    observability_status = "healthy" if tracing_ok and errors_ok else "degraded" if tracing_ok or errors_ok else "unavailable"
    optional_checks.append(
        _check(
            "observability",
            "Application observability",
            required=False,
            status=observability_status,
            summary=(
                "Tracing and error reporting are configured."
                if observability_status == "healthy"
                else "Only part of the tracing and error-reporting stack is configured."
                if observability_status == "degraded"
                else "Tracing and error reporting are not configured."
            ),
            remediation=(
                None
                if observability_status == "healthy"
                else "Configure both tracing credentials and the error-reporting DSN."
            ),
            metadata={"tracing_configured": tracing_ok, "error_reporting_configured": errors_ok},
        )
    )

    blockers = [
        {"id": item["id"], "label": item["label"], "status": item["status"]}
        for item in required_checks
        if item["status"] != "healthy"
    ]
    can_complete = not blockers
    optional_degraded = sum(
        item["status"] != "healthy" for item in optional_checks
    )
    status = "ready" if can_complete and not optional_degraded else "degraded" if can_complete else "blocked"

    all_checks = required_checks + optional_checks
    if not can_admin:
        for item in all_checks:
            item.pop("remediation", None)

    return {
        "status": status,
        "can_complete_onboarding": can_complete,
        "environment": settings.environment,
        "checked_at": _iso(_utcnow()),
        "required": required_checks,
        "optional": optional_checks,
        "blockers": blockers,
        "summary": {
            "required_healthy": len(required_checks) - len(blockers),
            "required_total": len(required_checks),
            "optional_degraded": optional_degraded,
            "optional_total": len(optional_checks),
        },
        "admin_actions_available": can_admin,
    }
