from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import importlib.util
import time
from typing import Any, Callable

import httpx

from core.config import settings


ConnectorHealth = dict[str, dict[str, Any]]
_CACHE: tuple[float, ConnectorHealth] | None = None
_CACHE_TTL_SECONDS = 30.0
_FORCED_REFRESH_MIN_INTERVAL_SECONDS = 10.0
_VERIFICATION_STALE_SECONDS = 300.0
_PROBE_TIMEOUT_SECONDS = 5.0
_LAST_VERIFIED: dict[str, datetime] = {}

_COMPOSIO_CORE_PROVIDERS = {
    "gmail": "Gmail",
    "slack": "Slack",
    "github": "GitHub",
    "google_drive": "Google Drive",
}


@dataclass(frozen=True)
class ProbeResult:
    """Secret-free result from a non-mutating provider credential check."""

    ok: bool
    checked_at: datetime
    latency_ms: int
    error_code: str | None = None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat().replace("+00:00", "Z") if value else None


def _module_available(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def _probe_failure(*, started: float, error_code: str) -> ProbeResult:
    return ProbeResult(
        ok=False,
        checked_at=_utcnow(),
        latency_ms=max(0, int((time.perf_counter() - started) * 1000)),
        error_code=error_code,
    )


async def _http_get_probe(
    *,
    url: str,
    headers: dict[str, str],
    params: dict[str, str | int] | None = None,
    response_ok: Callable[[httpx.Response], bool] | None = None,
) -> ProbeResult:
    """Perform a bounded GET probe without returning bodies, headers, or secrets.

    Provider error bodies are intentionally never read into the health contract:
    some services echo request details, so exposing raw exceptions could disclose a
    credential. Only fixed error codes derived from the response class are returned.
    """

    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(_PROBE_TIMEOUT_SECONDS),
            follow_redirects=False,
        ) as client:
            response = await client.get(url, headers=headers, params=params)
    except httpx.TimeoutException:
        return _probe_failure(started=started, error_code="timeout")
    except httpx.RequestError:
        return _probe_failure(started=started, error_code="network_error")
    except Exception:  # noqa: BLE001 - never expose provider/client exception text
        return _probe_failure(started=started, error_code="probe_error")

    checked_at = _utcnow()
    latency_ms = max(0, int((time.perf_counter() - started) * 1000))
    if response.status_code in {401, 403}:
        return ProbeResult(False, checked_at, latency_ms, "auth_rejected")
    if response.status_code == 429:
        return ProbeResult(False, checked_at, latency_ms, "rate_limited")
    if response.status_code >= 500:
        return ProbeResult(False, checked_at, latency_ms, "provider_unavailable")
    if response.status_code < 200 or response.status_code >= 300:
        return ProbeResult(False, checked_at, latency_ms, "unexpected_response")
    if response_ok is not None:
        try:
            valid = response_ok(response)
        except Exception:  # noqa: BLE001 - malformed provider payload, no body leakage
            valid = False
        if not valid:
            return ProbeResult(False, checked_at, latency_ms, "unexpected_response")
    return ProbeResult(True, checked_at, latency_ms)


def _json_is_list(response: httpx.Response) -> bool:
    return isinstance(response.json(), list)


def _json_is_collection(response: httpx.Response) -> bool:
    payload = response.json()
    return isinstance(payload, list) or (
        isinstance(payload, dict)
        and any(key in payload for key in ("items", "data", "next_page_token"))
    )


def _json_has_data_object(response: httpx.Response) -> bool:
    payload = response.json()
    return isinstance(payload, dict) and isinstance(payload.get("data"), dict)


async def _probe_browserbase() -> ProbeResult:
    # Browserbase documents GET /v1/projects as an authenticated, read-only call.
    def _project_is_available(response: httpx.Response) -> bool:
        payload = response.json()
        if not isinstance(payload, list):
            return False
        project_id = settings.browserbase_project_id.strip()
        if not project_id:
            return True
        return any(
            isinstance(project, dict) and str(project.get("id") or "") == project_id
            for project in payload
        )

    return await _http_get_probe(
        url="https://api.browserbase.com/v1/projects",
        headers={"x-bb-api-key": settings.browserbase_api_key.strip()},
        response_ok=_project_is_available,
    )


async def _probe_e2b() -> ProbeResult:
    # Listing at most one sandbox verifies the key without creating or resuming one.
    return await _http_get_probe(
        url="https://api.e2b.app/sandboxes",
        headers={"X-API-Key": settings.e2b_api_key.strip()},
        params={"limit": 1},
        response_ok=_json_is_collection,
    )


async def _probe_composio() -> ProbeResult:
    # Connected-account listing is read-only and responses redact stored tokens.
    return await _http_get_probe(
        url="https://backend.composio.dev/api/v3/connected_accounts",
        headers={"x-api-key": settings.composio_api_key.strip()},
        params={"limit": 1},
        response_ok=_json_is_collection,
    )


async def _probe_openrouter() -> ProbeResult:
    # GET /key is OpenRouter's documented, non-generating credential/limit
    # inspection endpoint. The response body is validated but never exposed.
    from connectors.openrouter_multimodal import openrouter_api_url

    return await _http_get_probe(
        url=openrouter_api_url(settings.openrouter_api_base, "key"),
        headers={"Authorization": f"Bearer {settings.openrouter_api_key.strip()}"},
        response_ok=_json_has_data_object,
    )


async def _safe_probe(provider: str, probe: Callable[[], Any]) -> ProbeResult:
    """Contain unexpected monkeypatch/SDK failures behind a redacted error code."""

    started = time.perf_counter()
    try:
        result = await probe()
    except Exception:  # noqa: BLE001 - exception messages can contain credentials
        return _probe_failure(started=started, error_code="probe_error")
    if not isinstance(result, ProbeResult):
        return _probe_failure(started=started, error_code="probe_error")
    return result


async def _playwright_available() -> tuple[bool, str]:
    if not _module_available("playwright"):
        return False, "Playwright is not installed."
    try:
        from playwright.async_api import async_playwright  # type: ignore[import]

        playwright = await async_playwright().start()
        try:
            browser = await playwright.chromium.launch(headless=True, args=["--no-sandbox"])
            await browser.close()
        finally:
            await playwright.stop()
    except Exception:  # noqa: BLE001 - browser launch details are operationally noisy
        return False, "Playwright Chromium could not launch."
    return True, "Playwright Chromium launched successfully."


def _probe_reason(provider: str, result: ProbeResult) -> str:
    if result.ok:
        return f"{provider} credentials were accepted by a read-only provider API check."
    messages = {
        "auth_rejected": f"{provider} rejected the configured credential.",
        "rate_limited": f"{provider} rate-limited the verification request.",
        "timeout": f"{provider} did not answer the verification request before the timeout.",
        "network_error": f"Chronos could not reach {provider} for verification.",
        "provider_unavailable": f"{provider} reported a temporary service failure during verification.",
        "unexpected_response": f"{provider} returned an unexpected response to the verification request.",
        "dependency_missing": f"{provider} credentials are present, but the required runtime package is missing.",
        "probe_error": f"Chronos could not complete the {provider} verification check.",
    }
    return messages.get(result.error_code or "", f"{provider} verification failed.")


def _verified_health(
    provider_key: str,
    provider_label: str,
    *,
    configured: bool,
    result: ProbeResult | None,
    setup: str,
) -> dict[str, Any]:
    if not configured:
        _LAST_VERIFIED.pop(provider_key, None)
        return {
            "status": "unavailable",
            "tier": "unavailable",
            "configured": False,
            "verified": False,
            "checked_at": None,
            "verified_at": None,
            "stale": True,
            "latency_ms": None,
            "error_code": "not_configured",
            "reason": f"{provider_label} credentials are not configured.",
            "setup": setup,
        }

    if result is None:
        return {
            "status": "configured",
            "tier": "configured",
            "configured": True,
            "verified": False,
            "checked_at": None,
            "verified_at": _iso(_LAST_VERIFIED.get(provider_key)),
            "stale": True,
            "latency_ms": None,
            "error_code": "not_checked",
            "reason": f"{provider_label} credentials are configured but have not been verified.",
            "setup": None,
        }

    if result.ok:
        _LAST_VERIFIED[provider_key] = result.checked_at
    verified_at = _LAST_VERIFIED.get(provider_key)
    stale = verified_at is None or (
        result.checked_at - verified_at
    ).total_seconds() > _VERIFICATION_STALE_SECONDS
    if result.ok:
        status = "verified"
        tier = "live"
    elif result.error_code == "auth_rejected":
        status = "error"
        tier = "degraded"
    else:
        status = "degraded"
        tier = "degraded"
    return {
        "status": status,
        "tier": tier,
        "configured": True,
        "verified": result.ok,
        "checked_at": _iso(result.checked_at),
        "verified_at": _iso(verified_at),
        "stale": stale,
        "latency_ms": result.latency_ms,
        "error_code": result.error_code,
        "reason": _probe_reason(provider_label, result),
        "setup": (
            "Replace or correct the credential, then refresh verification."
            if result.error_code == "auth_rejected"
            else None
        ),
    }


def _configured_only_health(label: str, *, configured: bool, setup: str) -> dict[str, Any]:
    if not configured:
        return {
            "status": "fixture",
            "tier": "fixture",
            "configured": False,
            "verified": False,
            "checked_at": None,
            "verified_at": None,
            "stale": True,
            "latency_ms": None,
            "error_code": "not_configured",
            "reason": f"{label} credentials are not configured; live actions are unavailable.",
            "setup": setup,
        }
    return {
        "status": "configured",
        "tier": "configured",
        "configured": True,
        "verified": False,
        "checked_at": None,
        "verified_at": None,
        "stale": True,
        "latency_ms": None,
        "error_code": "oauth_required",
        "reason": (
            f"{label} OAuth client credentials are configured. The provider can only "
            "verify them during a user consent and token exchange."
        ),
        "setup": "Connect an account, then run a read action to verify the user grant.",
    }


def _local_health(*, reason: str, setup: str | None = None) -> dict[str, Any]:
    return {
        "status": "live",
        "tier": "live",
        "configured": True,
        "verified": True,
        "checked_at": None,
        "verified_at": None,
        "stale": False,
        "latency_ms": None,
        "error_code": None,
        "reason": reason,
        "setup": setup,
        "verification_source": "local_runtime",
    }


def _unavailable_local(*, reason: str, setup: str) -> dict[str, Any]:
    return {
        "status": "unavailable",
        "tier": "unavailable",
        "configured": False,
        "verified": False,
        "checked_at": None,
        "verified_at": None,
        "stale": True,
        "latency_ms": None,
        "error_code": "not_configured",
        "reason": reason,
        "setup": setup,
    }


def _browser_health(
    *,
    browserbase: dict[str, Any],
    tavily: dict[str, Any],
    playwright_ok: bool,
    playwright_reason: str,
) -> dict[str, Any]:
    degraded: list[str] = []
    if browserbase["configured"] and browserbase["status"] != "verified":
        degraded.append("Browserbase")

    if browserbase["status"] == "verified":
        result = dict(browserbase)
        result.update(
            provider="browserbase",
            reason="Browserbase search credentials passed a read-only verification check.",
        )
        return result
    if playwright_ok:
        result = _local_health(reason=playwright_reason)
        result.update(
            provider="playwright",
            degraded_providers=degraded,
            reason=(
                f"{playwright_reason} Browserbase is degraded, so live browser tools use the "
                "verified local fallback."
                if degraded
                else f"{playwright_reason} Live browser tools use the local runtime."
            ),
        )
        return result
    if tavily["configured"]:
        result = dict(tavily)
        result.update(provider="tavily")
        return result
    if browserbase["configured"]:
        result = dict(browserbase)
        result.update(provider="browserbase")
        return result
    return {
        **_configured_only_health(
            "Browser search",
            configured=False,
            setup=(
                "Set TAVILY_API_KEY or BROWSERBASE_API_KEY, or install Playwright Chromium."
            ),
        ),
        "reason": "No verified live browser/search provider is available; browser.search uses fixtures.",
    }


def _browser_operator_health(browserbase: dict[str, Any]) -> dict[str, Any]:
    configured = bool(
        settings.browserbase_operator_enabled
        and settings.browserbase_api_key.strip()
        and settings.browserbase_project_id.strip()
    )
    if configured:
        result = dict(browserbase)
        result.update(
            provider="browserbase",
            configured=True,
            reason=(
                "Browserbase remote operator credentials and project passed verification; "
                "sessions use encrypted Contexts and replica-safe reconnects."
                if browserbase["status"] == "verified"
                else "Browserbase remote operator verification failed; production browser actions fail closed."
            ),
            setup=(
                "Verify BROWSERBASE_API_KEY, BROWSERBASE_PROJECT_ID, plan support for Contexts/keepAlive, and refresh."
            ),
        )
        return result
    if settings.is_production:
        return _unavailable_local(
            reason="Production browser operator is disabled because Browserbase remote execution is incomplete.",
            setup=(
                "Set BROWSERBASE_OPERATOR_ENABLED=true, BROWSERBASE_API_KEY, and BROWSERBASE_PROJECT_ID."
            ),
        )
    return _local_health(
        reason="Development browser operator uses sandboxed process-local Chromium without persisted login state."
    )


def _copy_remote_health(entry: dict[str, Any], *, reason: str, setup: str) -> dict[str, Any]:
    result = dict(entry)
    result["reason"] = reason
    if not result.get("configured"):
        result["setup"] = setup
    return result


def _model_capability_health(
    *,
    label: str,
    model: str,
    env_name: str,
    openrouter: dict[str, Any],
) -> dict[str, Any]:
    model = model.strip()
    if not model:
        return {
            **_unavailable_local(
                reason=f"{label} is disabled because {env_name} is not configured.",
                setup=f"Set {env_name} to a supported provider model.",
            ),
            "model": None,
        }
    if model.lower().startswith("openrouter/"):
        entry = dict(openrouter)
        entry["model"] = model
        if entry.get("configured"):
            entry["reason"] = (
                f"{label} uses {model}; the shared OpenRouter credential is "
                f"{entry['status']}."
            )
        else:
            entry["reason"] = (
                f"{label} is set to {model}, but OPENROUTER_API_KEY is not configured."
            )
            entry["setup"] = "Set OPENROUTER_API_KEY."
        return entry
    return {
        "status": "configured",
        "tier": "configured",
        "configured": True,
        "verified": False,
        "checked_at": None,
        "verified_at": None,
        "stale": True,
        "latency_ms": None,
        "error_code": "runtime_verification_required",
        "reason": (
            f"{label} uses {model}. Non-OpenRouter providers are verified on the first real call."
        ),
        "setup": None,
        "model": model,
    }


def _voice_capability_health(
    stt: dict[str, Any],
    tts: dict[str, Any],
) -> dict[str, Any]:
    stt_live = stt.get("tier") == "live"
    tts_live = tts.get("tier") == "live"
    if stt_live and tts_live:
        status, tier, error_code = "live", "live", None
    elif stt_live or tts_live:
        # Keep the native provider family executable while the individual tool
        # with a missing model returns its own honest unavailable result.
        status, tier, error_code = "degraded", "live", "partial_configuration"
    elif stt.get("configured") or tts.get("configured"):
        status, tier, error_code = "degraded", "degraded", "provider_unverified"
    else:
        status, tier, error_code = "unavailable", "unavailable", "not_configured"
    return {
        "status": status,
        "tier": tier,
        "configured": bool(stt.get("configured") or tts.get("configured")),
        "verified": bool(stt.get("verified") and tts.get("verified")),
        "checked_at": stt.get("checked_at") or tts.get("checked_at"),
        "verified_at": stt.get("verified_at") or tts.get("verified_at"),
        "stale": bool(stt.get("stale") or tts.get("stale")),
        "latency_ms": max(stt.get("latency_ms") or 0, tts.get("latency_ms") or 0) or None,
        "error_code": error_code,
        "reason": (
            f"Speech-to-text is {stt['status']}; text-to-speech is {tts['status']}."
        ),
        "setup": (
            None
            if stt_live and tts_live
            else "Configure and verify both STT_MODEL and TTS_MODEL provider paths."
        ),
        "capabilities": {"transcription": stt, "speech": tts},
    }


def _oauth_health(*, composio_on: bool, composio: dict[str, Any]) -> ConnectorHealth:
    from connectors.oauth_apps import available_apps

    health: ConnectorHealth = {}
    for app in available_apps():
        provider = str(app["id"])
        label = str(app["name"])
        if composio_on and provider in _COMPOSIO_CORE_PROVIDERS:
            entry = dict(composio)
            entry.update(
                auth="composio_managed",
                reason=(
                    f"Composio managed auth for {label} is {entry['status']}. Each user still "
                    "needs an active connected account before Chronos has account access."
                ),
                setup=f"Connect {label} to bind the current user to a Composio account.",
            )
            health[provider] = entry
            continue

        configured = bool(app.get("configured"))
        entry = _configured_only_health(
            label,
            configured=configured,
            setup=(
                f"Set {app.get('client_id_env') or 'CLIENT_ID'} and "
                f"{app.get('client_secret_env') or 'CLIENT_SECRET'}."
            ),
        )
        entry["auth"] = "direct_oauth"
        if provider == "gmail" and not configured:
            entry.update(
                status="demo",
                tier="demo",
                reason="Gmail is not configured; draft actions use local demo storage.",
            )
        health[provider] = entry
    return health


async def check_connectors(*, refresh: bool = False) -> ConnectorHealth:
    global _CACHE
    now = time.monotonic()
    if _CACHE:
        cache_age = now - _CACHE[0]
        if not refresh and cache_age < _CACHE_TTL_SECONDS:
            return _CACHE[1]
        # A manual refresh still gets a bounded cadence so an authenticated
        # caller cannot turn the status endpoint into provider-request abuse.
        if refresh and cache_age < _FORCED_REFRESH_MIN_INTERVAL_SECONDS:
            return _CACHE[1]

    from connectors.composio_client import is_configured as _composio_configured

    browserbase_configured = bool(settings.browserbase_api_key.strip())
    e2b_configured = bool(settings.e2b_api_key.strip())
    composio_key_configured = bool(settings.composio_api_key.strip())
    openrouter_configured = bool(settings.openrouter_api_key.strip())

    tasks: dict[str, Any] = {}
    if browserbase_configured:
        tasks["browserbase"] = _safe_probe("browserbase", _probe_browserbase)
    if e2b_configured:
        tasks["e2b"] = _safe_probe("e2b", _probe_e2b)
    if composio_key_configured:
        tasks["composio"] = _safe_probe("composio", _probe_composio)
    if openrouter_configured:
        tasks["openrouter"] = _safe_probe("openrouter", _probe_openrouter)
    keys = list(tasks)
    values = await asyncio.gather(*(tasks[key] for key in keys))
    probes = dict(zip(keys, values, strict=True))

    browserbase = _verified_health(
        "browserbase",
        "Browserbase",
        configured=browserbase_configured,
        result=probes.get("browserbase"),
        setup="Set BROWSERBASE_API_KEY.",
    )
    tavily = _configured_only_health(
        "Tavily",
        configured=bool(settings.tavily_api_key.strip()),
        setup="Set TAVILY_API_KEY.",
    )
    if tavily["configured"]:
        tavily["reason"] = (
            "Tavily credentials are configured. Tavily has no non-consuming credential check, "
            "so the first real search is the verification boundary."
        )
        tavily["error_code"] = "runtime_verification_required"

    if browserbase["status"] == "verified":
        playwright_ok, playwright_reason = False, "Local browser fallback was not checked."
    else:
        playwright_ok, playwright_reason = await _playwright_available()

    e2b = _verified_health(
        "e2b",
        "E2B",
        configured=e2b_configured,
        result=probes.get("e2b"),
        setup="Set E2B_API_KEY (and optionally E2B_TEMPLATE_ID).",
    )
    composio = _verified_health(
        "composio",
        "Composio",
        configured=composio_key_configured,
        result=probes.get("composio"),
        setup="Set COMPOSIO_API_KEY and install the composio SDK.",
    )
    openrouter = _verified_health(
        "openrouter",
        "OpenRouter",
        configured=openrouter_configured,
        result=probes.get("openrouter"),
        setup="Set OPENROUTER_API_KEY.",
    )
    composio_on = _composio_configured()
    if composio_key_configured and not composio_on:
        composio.update(
            status="error",
            tier="degraded",
            verified=False,
            error_code="dependency_missing",
            reason=_probe_reason(
                "Composio",
                ProbeResult(False, _utcnow(), 0, "dependency_missing"),
            ),
            setup="Install the pinned composio SDK used by Chronos.",
        )

    browser = _browser_health(
        browserbase=browserbase,
        tavily=tavily,
        playwright_ok=bool(playwright_ok),
        playwright_reason=str(playwright_reason),
    )
    browser_operator = _browser_operator_health(browserbase)

    host_execution_ok = not settings.is_production
    isolated_network = (
        "internet access explicitly enabled"
        if settings.e2b_allow_internet_access
        else "internet access disabled"
    )
    if host_execution_ok:
        code = _local_health(
            reason="Development-only Python subprocess execution is available with resource limits."
        )
        data = _local_health(
            reason="Development-only data-analysis subprocess execution is available."
        )
    else:
        code = _copy_remote_health(
            e2b,
            reason=(
                f"Python executes in ephemeral E2B sandboxes with {isolated_network}; provider verification "
                f"is {e2b['status']}."
            ),
            setup="Set and verify E2B_API_KEY (and optionally E2B_TEMPLATE_ID).",
        )
        data = _copy_remote_health(
            e2b,
            reason=(
                f"Data analysis executes in ephemeral E2B sandboxes with {isolated_network}; provider "
                f"verification is {e2b['status']}."
            ),
            setup="Set and verify E2B_API_KEY and configure an analysis template.",
        )

    from core.egress_policy import parse_egress_allowlist

    computer_allowlist = parse_egress_allowlist(settings.e2b_computer_egress_allowlist)
    if settings.e2b_computer_allow_internet_access and not computer_allowlist:
        computer = _unavailable_local(
            reason="Cloud-computer network access is enabled without an egress allowlist.",
            setup="Set E2B_COMPUTER_EGRESS_ALLOWLIST to the organization-approved domain ceiling.",
        )
    else:
        computer = _copy_remote_health(
            e2b,
            reason=(
                f"E2B isolated computer runtime verification is {e2b['status']}; "
                "network-enabled sessions are provider-enforced per-domain allowlists."
            ),
            setup="Set and verify E2B_API_KEY and E2B_COMPUTER_EGRESS_ALLOWLIST.",
        )
    skill = _copy_remote_health(
        e2b,
        reason=f"Bundled skill scripts use E2B; provider verification is {e2b['status']}.",
        setup="Set and verify E2B_API_KEY.",
    )
    repo_configured = bool(
        settings.e2b_repo_enabled
        and settings.e2b_repo_template_id.strip()
        and settings.e2b_repo_allow_internet_access
        and parse_egress_allowlist(settings.e2b_repo_egress_allowlist)
    )
    if host_execution_ok:
        repo_health = _local_health(
            reason=(
                "Development-only repo workspaces are available with branch, file, pytest, "
                "and diff tools."
            )
        )
    elif repo_configured:
        repo_health = _copy_remote_health(
            e2b,
            reason=(
                "Persistent repo workspaces use tenant/task-scoped E2B sandboxes, Postgres "
                f"leases, and S3 snapshots; provider verification is {e2b['status']}."
            ),
            setup=(
                "Verify E2B_API_KEY and the E2B repo template includes git, Python, and pytest."
            ),
        )
    else:
        repo_health = _unavailable_local(
            reason="Persistent isolated repository runtime is not fully configured.",
            setup=(
                "Set E2B_REPO_ENABLED=true, E2B_REPO_TEMPLATE_ID, and "
                "E2B_REPO_ALLOW_INTERNET_ACCESS=true, plus E2B_REPO_EGRESS_ALLOWLIST."
            ),
        )

    host_execution_reason = (
        "Available only in development/test; production requires a separate isolated runtime "
        "and never executes user-controlled processes in the API container."
    )
    if host_execution_ok:
        local_computer = _local_health(
            reason="Development-only local-folder bridge is available."
        )
    else:
        from core.desktop_bridge import desktop_bridge

        local_computer = await desktop_bridge.health()
    vision = _model_capability_health(
        label="Vision/OCR",
        model=settings.vision_model,
        env_name="VISION_MODEL",
        openrouter=openrouter,
    )
    image = _model_capability_health(
        label="Image generation and full-image editing",
        model=settings.image_model,
        env_name="IMAGE_MODEL",
        openrouter=openrouter,
    )
    stt = _model_capability_health(
        label="Speech-to-text",
        model=settings.stt_model,
        env_name="STT_MODEL",
        openrouter=openrouter,
    )
    tts = _model_capability_health(
        label="Text-to-speech",
        model=settings.tts_model,
        env_name="TTS_MODEL",
        openrouter=openrouter,
    )
    health: ConnectorHealth = {
        "browser": browser,
        "browser_operator": browser_operator,
        "browserbase": browserbase,
        "tavily": tavily,
        "e2b": e2b,
        "composio": composio,
        "openrouter": openrouter,
        "vision": vision,
        "image": image,
        "stt": stt,
        "tts": tts,
        "voice": _voice_capability_health(stt, tts),
        "fs": _local_health(
            reason="Task workspace filesystem tools are available with a per-task path jail."
        ),
        "code": code,
        "data": data,
        "computer": computer,
        "local_computer": local_computer,
        "desktop": (
            _local_health(reason="Development-only API-host virtual desktop is available.")
            if host_execution_ok
            else _unavailable_local(
                reason=host_execution_reason,
                setup="Use an isolated desktop runtime outside the API container.",
            )
        ),
        "skill": skill,
        "repo": repo_health,
        "mcp": {
            **_local_health(
                reason=(
                    "MCP servers can be registered and discovered; execution still requires a "
                    "reachable configured server."
                )
            ),
            "status": "available",
            "setup": "Register an MCP server before using mcp.<server_id>.<tool>.",
        },
    }
    health.update(_oauth_health(composio_on=composio_on, composio=composio))
    _CACHE = (now, health)
    return health


async def connector_tier(provider: str) -> str:
    if settings.demo_mode:
        return "demo"
    health = await check_connectors()
    return str(health.get(provider, {}).get("tier") or "fixture")


async def degraded_note(provider: str) -> str | None:
    """Return a placeholder warning only for genuinely non-live result tiers.

    ``configured`` and ``degraded`` providers still execute their real connector
    path, so they must not be mislabeled as fixture data merely because the last
    control-plane verification was inconclusive.
    """

    health = await check_connectors()
    entry = health.get(provider)
    if not entry or str(entry.get("tier")) not in {"demo", "fixture", "unavailable"}:
        return None
    return str(
        entry.get("reason")
        or f"{provider} is not fully configured and returns placeholder (non-real) results."
    )
