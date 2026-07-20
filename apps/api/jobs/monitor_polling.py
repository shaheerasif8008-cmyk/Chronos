"""Durable, tenant-safe polling for Chronos monitors.

The API scheduler leader calls :func:`run_due_monitors`, while Postgres claims,
idempotent run keys, and deduplicated alerts remain the correctness boundary
across replicas, restarts, and a degraded Redis lock.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
import hashlib
import json
import logging
import re
import uuid
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
import httpx
from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from core import audit, notifications, tool_broker
from core.config import settings
from core.db import engine, reflect_table
from core.models import AgentContext
from core.ssrf import UnsafeURLError, assert_safe_url
from core.untrusted_content import scan_untrusted_content


log = logging.getLogger(__name__)
scheduler = AsyncIOScheduler()

_RUNNING_STALE_SECONDS = 180
_MAX_POLL_CANDIDATES = 500
_MAX_PERSISTED_SNIPPET = 1_000
_MAX_SCAN_TEXT = 10_000
_MAX_CONNECTOR_CANONICAL_BYTES = 250_000
_TEXT_CONTENT_TYPES = (
    "text/",
    "application/json",
    "application/xml",
    "application/xhtml+xml",
    "application/rss+xml",
    "application/atom+xml",
)
_READ_ONLY_TOOL_DENY = re.compile(
    r"(?:^|[._-])(send|post|publish|create|update|delete|remove|upload|write|move|copy|execute|draft|archive|reply|invite|approve|mark|set)(?:$|[._-])",
    re.IGNORECASE,
)
_READ_ONLY_TOOL_ALLOW = re.compile(
    r"(?:^|[._-])(search|list|get|read|fetch|query|find|lookup|inspect|retrieve)(?:$|[._-])",
    re.IGNORECASE,
)
_SECRET_ARGUMENT_KEY = re.compile(
    r"(?:^|_)(authorization|cookie|password|api_?key|access_?token|refresh_?token|secret)(?:$|_)",
    re.IGNORECASE,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def _clean_text(value: str) -> str:
    return " ".join(str(value or "").split())


def _safe_error_summary(message: str) -> str:
    cleaned = _clean_text(message)[:1_000]
    cleaned = re.sub(r"https?://\S+", "[redacted-url]", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"(?i)\bBearer\s+\S+", "Bearer [redacted]", cleaned)
    cleaned = re.sub(
        r"(?i)\b(authorization|cookie|set-cookie|x-api-key|api[_ -]?key|token|secret|password)\b\s*[:=]\s*\S+",
        r"\1=[redacted]",
        cleaned,
    )
    return cleaned[:240] or "Monitor polling failed."


def validate_read_only_tool(tool: str, args: dict[str, Any]) -> None:
    """Reject background connector specs that can mutate an external system."""

    if not tool or _READ_ONLY_TOOL_DENY.search(tool):
        raise ValueError("Monitor connector tools must be read-only search, list, or get actions")
    method = str(args.get("method") or "GET").upper()
    if method not in {"GET", "HEAD"}:
        raise ValueError("Monitor connector HTTP actions must use GET or HEAD")
    if not _READ_ONLY_TOOL_ALLOW.search(tool) and "http" not in tool.lower():
        raise ValueError("Monitor connector tools must explicitly identify a read-only action")

    def contains_secret(value: Any) -> bool:
        if isinstance(value, dict):
            return any(
                _SECRET_ARGUMENT_KEY.search(str(key).lower().replace("-", "_"))
                or contains_secret(item)
                for key, item in value.items()
            )
        if isinstance(value, (list, tuple)):
            return any(contains_secret(item) for item in value)
        return False

    if contains_secret(args):
        raise ValueError("Monitor source arguments cannot contain credentials; use the connector vault")


class MonitorPollError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool,
        degraded: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.degraded = degraded


class _ReadableHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self._in_title = False
        self._ignored = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag in {"script", "style", "noscript", "svg"}:
            self._ignored += 1
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg"} and self._ignored:
            self._ignored -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._ignored:
            return
        text = data.strip()
        if not text:
            return
        if self._in_title and not self.title:
            self.title = _clean_text(text)[:300]
        self._parts.append(text)

    @property
    def text(self) -> str:
        return _clean_text(" ".join(self._parts))


def _public_observation(observation: dict[str, Any]) -> dict[str, Any]:
    """Strip internal matching text and bound every persisted external field."""

    result = {
        key: value
        for key, value in observation.items()
        if not key.startswith("_") and key not in {"content", "raw", "body"}
    }
    for key in ("title", "snippet", "url", "provider"):
        if result.get(key) is not None:
            limit = _MAX_PERSISTED_SNIPPET if key == "snippet" else 500
            result[key] = str(result[key])[:limit]
    return result


def _next_run(monitor: dict[str, Any], after: datetime) -> datetime:
    interval = max(
        settings.monitor_min_interval_seconds,
        min(
            settings.monitor_max_interval_seconds,
            int(monitor.get("interval_seconds") or 900),
        ),
    )
    seed = int(_hash(f"{monitor.get('id')}:{after.date().isoformat()}")[:8], 16)
    jitter = seed % max(1, int(interval * 0.1) + 1)
    return after + timedelta(seconds=interval + jitter)


def _retry_at(monitor: dict[str, Any], attempt: int, after: datetime) -> datetime:
    base = min(3_600, 30 * (2 ** max(0, attempt - 1)))
    seed = int(_hash(f"{monitor.get('id')}:{attempt}")[:8], 16)
    return after + timedelta(seconds=base + seed % max(1, base // 5 + 1))


async def _website_observation(monitor: dict[str, Any]) -> dict[str, Any]:
    url = str(monitor.get("target") or "").strip()
    try:
        assert_safe_url(url)
    except UnsafeURLError as exc:
        raise MonitorPollError("unsafe_target", str(exc), retryable=False) from exc

    headers = {
        "Accept": "text/html,application/xhtml+xml,application/json,text/plain,application/xml;q=0.9,*/*;q=0.1",
        "User-Agent": "ChronosMonitor/1.0",
    }
    if monitor.get("last_etag"):
        headers["If-None-Match"] = str(monitor["last_etag"])
    if monitor.get("last_modified"):
        headers["If-Modified-Since"] = str(monitor["last_modified"])

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(settings.monitor_fetch_timeout_seconds),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            async with client.stream("GET", url, headers=headers) as response:
                if response.status_code == 304:
                    return {
                        "url": url,
                        "hash": monitor.get("content_hash"),
                        "not_modified": True,
                        "http_status": 304,
                        "etag": monitor.get("last_etag"),
                        "last_modified": monitor.get("last_modified"),
                        "observed_at": _now().isoformat(),
                    }
                if response.is_redirect:
                    raise MonitorPollError(
                        "redirect_blocked",
                        "Monitor targets may not redirect; configure the final public URL.",
                        retryable=False,
                    )
                if response.status_code == 429:
                    raise MonitorPollError("rate_limited", "The website rate-limited the monitor.", retryable=True)
                if response.status_code >= 500:
                    raise MonitorPollError("upstream_unavailable", "The website reported a server error.", retryable=True)
                if response.status_code >= 400:
                    raise MonitorPollError(
                        f"http_{response.status_code}",
                        f"The website returned HTTP {response.status_code}.",
                        retryable=False,
                    )
                content_type = str(response.headers.get("content-type") or "").lower()
                if content_type and not any(content_type.startswith(kind) for kind in _TEXT_CONTENT_TYPES):
                    raise MonitorPollError(
                        "unsupported_content_type",
                        "The monitor target did not return readable text content.",
                        retryable=False,
                    )
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > settings.monitor_fetch_max_bytes:
                        raise MonitorPollError(
                            "response_too_large",
                            "The monitor response exceeded the configured byte limit.",
                            retryable=False,
                        )
                    chunks.append(chunk)
                raw = b"".join(chunks)
                encoding = response.encoding or "utf-8"
                text = raw.decode(encoding, errors="replace")
                if "html" in content_type or "xhtml" in content_type:
                    parser = _ReadableHTML()
                    parser.feed(text)
                    readable = parser.text
                    title = parser.title
                else:
                    readable = _clean_text(text)
                    title = ""
                digest = _hash(readable)
                scan = scan_untrusted_content(readable[:_MAX_SCAN_TEXT], source=f"monitor:{url}")
                return {
                    "url": url,
                    "title": title,
                    "snippet": readable[:_MAX_PERSISTED_SNIPPET],
                    "_match_text": readable[:_MAX_CONNECTOR_CANONICAL_BYTES],
                    "hash": digest,
                    "not_modified": False,
                    "http_status": response.status_code,
                    "etag": str(response.headers.get("etag") or "")[:500] or None,
                    "last_modified": str(response.headers.get("last-modified") or "")[:500] or None,
                    "bytes": size,
                    "untrusted_content": scan,
                    "observed_at": _now().isoformat(),
                }
    except MonitorPollError:
        raise
    except httpx.TimeoutException as exc:
        raise MonitorPollError("timeout", "The monitor fetch timed out.", retryable=True) from exc
    except httpx.RequestError as exc:
        raise MonitorPollError("network_error", "The monitor could not reach the website.", retryable=True) from exc


def _connector_payload_text(data: Any) -> tuple[str, str, int]:
    """Return deterministic hash input, a safe summary, and item count."""

    from jobs.source_sync import normalize_documents

    documents = normalize_documents(data if isinstance(data, (dict, list)) else {})
    if documents:
        canonical = [
            {
                "id": str(item.get("external_id") or "")[:500],
                "title": str(item.get("title") or "")[:500],
                "content": str(item.get("content") or "")[:20_000],
            }
            for item in documents[:100]
        ]
        encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"), default=str)
        titles = "; ".join(item["title"] for item in canonical if item["title"])
        return encoded[:_MAX_CONNECTOR_CANONICAL_BYTES], titles[:_MAX_PERSISTED_SNIPPET], len(canonical)
    encoded = json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)
    return encoded[:_MAX_CONNECTOR_CANONICAL_BYTES], "Connector data changed.", 1 if data else 0


def _provider_is_degraded(data: dict[str, Any]) -> bool:
    tier = str(data.get("tier") or "").lower()
    return bool(
        data.get("demo")
        or data.get("is_unavailable")
        or data.get("degraded_connector")
        or tier in {"demo", "fixture", "unavailable"}
    )


async def _tool_observation(monitor: dict[str, Any], *, news: bool = False) -> dict[str, Any]:
    source_config = monitor.get("source_config") if isinstance(monitor.get("source_config"), dict) else {}
    if news:
        tool = "browser.search"
        args = {
            "query": str(monitor.get("target") or "").strip(),
            "max_results": min(20, max(1, int(source_config.get("max_results") or 10))),
        }
    else:
        tool = str(source_config.get("tool") or "").strip()
        args = dict(source_config.get("args") or {})
        try:
            validate_read_only_tool(tool, args)
        except ValueError as exc:
            raise MonitorPollError("invalid_source_config", str(exc), retryable=False) from exc

    member_id = str(monitor.get("created_by") or "chronos")
    agent = AgentContext(
        id=f"monitor:{monitor['id']}",
        org_id=str(monitor["organization_id"]),
        member_id=member_id,
        workspace_id=str(source_config.get("workspace_id") or "default"),
    )
    try:
        result = await tool_broker.execute(agent, tool, args)
    except Exception as exc:
        code = "provider_not_authorized" if exc.__class__.__name__ in {"ApprovalRequired", "ConnectorNotFound"} else "provider_error"
        raise MonitorPollError(code, _safe_error_summary(str(exc)), retryable=code == "provider_error", degraded=True) from exc

    data = dict(result.data or {})
    if _provider_is_degraded(data):
        raise MonitorPollError(
            "provider_degraded",
            str(data.get("degraded_connector") or data.get("warning") or result.summary),
            retryable=False,
            degraded=True,
        )
    canonical, summary, item_count = _connector_payload_text(data)
    scan = data.get("untrusted_content") or scan_untrusted_content(
        canonical[:_MAX_SCAN_TEXT], source=f"monitor:{tool}"
    )
    return {
        "title": str(monitor.get("name") or tool),
        "snippet": summary or result.summary[:_MAX_PERSISTED_SNIPPET],
        "_match_text": canonical,
        "hash": _hash(canonical),
        "provider": tool.split(".", 1)[0],
        "item_count": item_count,
        "untrusted_content": scan,
        "observed_at": _now().isoformat(),
    }


async def collect_observation(monitor: dict[str, Any]) -> dict[str, Any]:
    monitor_type = str(monitor.get("monitor_type") or "")
    if monitor_type == "website":
        return await _website_observation(monitor)
    if monitor_type in {"news", "digest"}:
        return await _tool_observation(monitor, news=True)
    if monitor_type in {"source", "connector", "inbox"}:
        return await _tool_observation(monitor)
    raise MonitorPollError("unsupported_monitor_type", "Unsupported monitor type.", retryable=False)


def _condition_matches(
    monitor: dict[str, Any], observation: dict[str, Any]
) -> tuple[bool, bool]:
    previous_hash = str(monitor.get("content_hash") or "")
    current_hash = str(observation.get("hash") or "")
    changed = bool(previous_hash and current_hash and previous_hash != current_hash)
    condition = monitor.get("condition") if isinstance(monitor.get("condition"), dict) else {}
    operator = str(condition.get("operator") or "changed")
    if observation.get("not_modified"):
        return False, False
    if operator == "changed":
        return changed, changed
    if operator == "contains":
        needle = str(condition.get("value") or "").lower().strip()
        haystack = str(observation.get("_match_text") or observation.get("snippet") or "").lower()
        return changed, bool(needle and needle in haystack)
    if operator == "always":
        return changed, True
    return changed, False


async def _claim_monitor(
    monitor_id: str,
    organization_id: str,
    *,
    now: datetime,
    require_due: bool,
) -> tuple[dict[str, Any], str] | None:
    monitors = await reflect_table("monitors")
    token = uuid.uuid4().hex
    conditions = [
        monitors.c.id == monitor_id,
        monitors.c.organization_id == organization_id,
        or_(monitors.c.lease_expires_at.is_(None), monitors.c.lease_expires_at <= now),
    ]
    if require_due:
        conditions.extend(
            [
                monitors.c.status == "active",
                or_(monitors.c.next_run_at.is_(None), monitors.c.next_run_at <= now),
                or_(monitors.c.backoff_until.is_(None), monitors.c.backoff_until <= now),
            ]
        )
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                update(monitors)
                .where(*conditions)
                .values(
                    lease_token=token,
                    lease_expires_at=now + timedelta(seconds=settings.monitor_lease_seconds),
                    updated_at=now,
                )
                .returning(monitors)
            )
        ).mappings().first()
    return (dict(row), token) if row else None


async def _release_monitor(monitor_id: str, token: str) -> None:
    monitors = await reflect_table("monitors")
    async with engine.begin() as conn:
        await conn.execute(
            update(monitors)
            .where(monitors.c.id == monitor_id, monitors.c.lease_token == token)
            .values(lease_token=None, lease_expires_at=None, updated_at=_now())
        )


async def _create_run(
    monitor: dict[str, Any],
    *,
    run_key: str,
    trigger_source: str,
    now: datetime,
) -> dict[str, Any] | None:
    runs = await reflect_table("monitor_runs")
    stmt = (
        pg_insert(runs)
        .values(
            organization_id=monitor["organization_id"],
            region=monitor.get("region") or settings.region,
            monitor_id=monitor["id"],
            run_key=run_key,
            trigger_source=trigger_source,
            status="running",
            attempt=1,
            started_at=now,
        )
        .on_conflict_do_nothing(constraint="uq_monitor_runs_monitor_key")
        .returning(runs)
    )
    async with engine.begin() as conn:
        row = (await conn.execute(stmt)).mappings().first()
    return dict(row) if row else None


async def _resume_retry_run(run: dict[str, Any], now: datetime) -> dict[str, Any]:
    runs = await reflect_table("monitor_runs")
    attempt = int(run.get("attempt") or 1) + 1
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                update(runs)
                .where(
                    runs.c.id == run["id"],
                    runs.c.organization_id == run["organization_id"],
                    runs.c.status == "retry",
                )
                .values(
                    status="running",
                    attempt=attempt,
                    started_at=now,
                    completed_at=None,
                    next_attempt_at=None,
                    error_code=None,
                    error_summary=None,
                )
                .returning(runs)
            )
        ).mappings().first()
    return dict(row) if row else run


async def _trigger_workflow(
    monitor: dict[str, Any], alert_id: str, evidence: dict[str, Any]
) -> str | None:
    workflow_id = str(monitor.get("workflow_id") or "")
    if not workflow_id:
        return None
    scan = evidence.get("untrusted_content") if isinstance(evidence.get("untrusted_content"), dict) else {}
    if scan.get("risk") == "prompt_injection":
        return None
    try:
        from connectors.framework.queue_factory import connector_execution_queue
        from connectors.framework.repository import DatabaseConnectorRepository
        from connectors.framework.workflows import WorkflowRuntime

        repo = DatabaseConnectorRepository()
        runtime = WorkflowRuntime(repo, connector_execution_queue())
        run = await runtime.start_run(
            workflow_id,
            tenant_id=str(monitor["organization_id"]),
            trigger_source="monitor",
            trigger_event_type="monitor.changed",
            trigger_payload={
                "monitor_id": str(monitor["id"]),
                "alert_id": alert_id,
                "evidence": evidence,
            },
            trigger_idempotency_key=f"monitor-alert:{alert_id}",
        )
        await runtime.tick(run["id"], tenant_id=str(monitor["organization_id"]))
        return str(run["id"])
    except Exception:
        log.exception("monitor workflow trigger failed for %s", monitor.get("id"))
        return None


async def _create_alert(
    monitor: dict[str, Any],
    run: dict[str, Any],
    observation: dict[str, Any],
) -> tuple[str | None, str | None]:
    alerts = await reflect_table("monitor_alerts")
    condition = monitor.get("condition") if isinstance(monitor.get("condition"), dict) else {}
    evidence = _public_observation(observation)
    scan = evidence.get("untrusted_content") if isinstance(evidence.get("untrusted_content"), dict) else {}
    if scan.get("risk") == "prompt_injection":
        summary = "External content changed; embedded instructions were quarantined as untrusted."
    else:
        summary = str(observation.get("snippet") or f"{monitor.get('name') or monitor.get('target')} changed")[:500]
    dedupe_key = _hash(
        json.dumps(
            {
                "monitor_id": str(monitor["id"]),
                "hash": observation.get("hash"),
                "condition": condition,
            },
            sort_keys=True,
            default=str,
        )
    )
    cooldown = max(0, min(86_400, int(monitor.get("alert_cooldown_seconds") or 0)))
    if cooldown:
        async with engine.begin() as conn:
            latest_alert_at = await conn.scalar(
                select(func.max(alerts.c.created_at)).where(
                    alerts.c.organization_id == monitor["organization_id"],
                    alerts.c.monitor_id == monitor["id"],
                )
            )
        latest = _as_utc(latest_alert_at)
        if latest and latest > _now() - timedelta(seconds=cooldown):
            return None, None
    stmt = (
        pg_insert(alerts)
        .values(
            organization_id=monitor["organization_id"],
            region=monitor.get("region") or settings.region,
            monitor_id=monitor["id"],
            run_id=run["id"],
            dedupe_key=dedupe_key,
            severity=str(condition.get("severity") or "info"),
            summary=summary,
            evidence=evidence,
            status="open",
        )
        .on_conflict_do_nothing(
            index_elements=[alerts.c.organization_id, alerts.c.monitor_id, alerts.c.dedupe_key]
        )
        .returning(alerts.c.id)
    )
    async with engine.begin() as conn:
        alert_id = (await conn.execute(stmt)).scalar_one_or_none()
    if alert_id is None:
        return None, None
    alert_id_text = str(alert_id)

    await audit.log(
        "monitor_alert_created",
        "monitor_poller",
        "monitors.poll",
        organization_id=str(monitor["organization_id"]),
        resource_type="monitor_alerts",
        resource_id=alert_id_text,
        payload={
            "monitor_id": str(monitor["id"]),
            "run_id": str(run["id"]),
            "severity": str(condition.get("severity") or "info"),
            "untrusted_risk": scan.get("risk"),
        },
    )
    try:
        await notifications.emit(
            organization_id=str(monitor["organization_id"]),
            type="monitor_alert",
            title=f"Monitor alert: {monitor.get('name') or monitor.get('target')}",
            body=summary,
            severity=(
                str(condition.get("severity"))
                if str(condition.get("severity")) in {"info", "success", "warning", "critical"}
                else "info"
            ),
            member_id=str(monitor.get("created_by") or "") or None,
            resource_type="monitor_alert",
            resource_id=alert_id_text,
            created_by="chronos",
        )
    except Exception:
        log.exception("monitor notification creation failed for alert %s", alert_id_text)
    workflow_run_id = await _trigger_workflow(monitor, alert_id_text, evidence)
    return alert_id_text, workflow_run_id


async def _finish_success(
    monitor: dict[str, Any],
    run: dict[str, Any],
    observation: dict[str, Any],
    *,
    now: datetime,
) -> dict[str, Any]:
    monitors = await reflect_table("monitors")
    runs = await reflect_table("monitor_runs")
    previous_hash = str(monitor.get("content_hash") or "")
    changed, matched = _condition_matches(monitor, observation)
    baseline = not previous_hash and bool(observation.get("hash"))
    alert_id = workflow_run_id = None
    if matched and not baseline:
        alert_id, workflow_run_id = await _create_alert(monitor, run, observation)
    status = "baseline" if baseline else "alerted" if alert_id else "changed" if changed else "no_change"
    public = _public_observation(observation)
    next_run_at = _next_run(monitor, now)
    monitor_values: dict[str, Any] = {
        "last_checked_at": now,
        "last_run_at": now,
        "last_success_at": now,
        "last_run_status": status,
        "last_error_code": None,
        "consecutive_failures": 0,
        "backoff_until": None,
        "next_run_at": next_run_at,
        "last_evidence": public,
        "updated_at": now,
    }
    if observation.get("hash"):
        monitor_values["content_hash"] = observation["hash"]
    if "etag" in observation:
        monitor_values["last_etag"] = observation.get("etag")
    if "last_modified" in observation:
        monitor_values["last_modified"] = observation.get("last_modified")
    if alert_id:
        monitor_values["alert_count"] = int(monitor.get("alert_count") or 0) + 1
    async with engine.begin() as conn:
        await conn.execute(
            update(monitors)
            .where(monitors.c.id == monitor["id"], monitors.c.organization_id == monitor["organization_id"])
            .values(**monitor_values)
        )
        await conn.execute(
            update(runs)
            .where(runs.c.id == run["id"], runs.c.organization_id == monitor["organization_id"])
            .values(
                status=status,
                completed_at=now,
                observation=public,
                content_hash=observation.get("hash"),
                alert_id=alert_id,
                workflow_run_id=workflow_run_id,
            )
        )
    await audit.log(
        "monitor_polled",
        "monitor_poller",
        "monitors.poll",
        organization_id=str(monitor["organization_id"]),
        resource_type="monitors",
        resource_id=str(monitor["id"]),
        payload={"run_id": str(run["id"]), "status": status, "alert_id": alert_id},
    )
    return {
        "id": str(run["id"]),
        "monitor_id": str(monitor["id"]),
        "status": status,
        "attempt": int(run.get("attempt") or 1),
        "alert_id": alert_id,
        "workflow_run_id": workflow_run_id,
        "observation": public,
        "next_run_at": next_run_at.isoformat(),
    }


async def _finish_failure(
    monitor: dict[str, Any],
    run: dict[str, Any],
    error: MonitorPollError,
    *,
    now: datetime,
) -> dict[str, Any]:
    monitors = await reflect_table("monitors")
    runs = await reflect_table("monitor_runs")
    attempt = int(run.get("attempt") or 1)
    max_attempts = max(1, min(10, int(monitor.get("max_attempts") or 5)))
    if error.degraded:
        status = "degraded"
        next_attempt = None
        next_run_at = _next_run(monitor, now)
    elif error.retryable and attempt < max_attempts:
        status = "retry"
        next_attempt = _retry_at(monitor, attempt, now)
        next_run_at = monitor.get("next_run_at") or _next_run(monitor, now)
    else:
        status = "dead_letter"
        next_attempt = None
        next_run_at = _next_run(monitor, now)
    error_summary = _safe_error_summary(str(error))
    async with engine.begin() as conn:
        await conn.execute(
            update(monitors)
            .where(monitors.c.id == monitor["id"], monitors.c.organization_id == monitor["organization_id"])
            .values(
                last_checked_at=now,
                last_run_at=now,
                last_failure_at=now,
                last_run_status=status,
                last_error_code=error.code,
                consecutive_failures=int(monitor.get("consecutive_failures") or 0) + 1,
                backoff_until=next_attempt,
                next_run_at=next_run_at,
                updated_at=now,
            )
        )
        await conn.execute(
            update(runs)
            .where(runs.c.id == run["id"], runs.c.organization_id == monitor["organization_id"])
            .values(
                status=status,
                completed_at=now,
                next_attempt_at=next_attempt,
                error_code=error.code,
                error_summary=error_summary,
            )
        )
    await audit.log(
        "monitor_poll_failed",
        "monitor_poller",
        "monitors.poll",
        organization_id=str(monitor["organization_id"]),
        resource_type="monitors",
        resource_id=str(monitor["id"]),
        payload={
            "run_id": str(run["id"]),
            "status": status,
            "error_code": error.code,
            "attempt": attempt,
        },
        decision="provider_degraded" if error.degraded else "poll_failed",
    )
    return {
        "id": str(run["id"]),
        "monitor_id": str(monitor["id"]),
        "status": status,
        "attempt": attempt,
        "error_code": error.code,
        "error_summary": error_summary,
        "next_attempt_at": next_attempt.isoformat() if next_attempt else None,
        "next_run_at": next_run_at.isoformat() if isinstance(next_run_at, datetime) else next_run_at,
    }


async def _execute_claimed(
    monitor: dict[str, Any],
    token: str,
    *,
    trigger_source: str,
    run_key: str | None = None,
    retry_run: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    now = now or _now()
    try:
        if retry_run:
            run = await _resume_retry_run(retry_run, now)
        else:
            key = run_key or f"scheduled:{int((_as_utc(monitor.get('next_run_at')) or now).timestamp())}"
            run = await _create_run(monitor, run_key=key, trigger_source=trigger_source, now=now)
            if run is None:
                return None
        try:
            observation = await collect_observation(monitor)
            return await _finish_success(monitor, run, observation, now=now)
        except MonitorPollError as exc:
            return await _finish_failure(monitor, run, exc, now=now)
        except Exception:  # noqa: BLE001 - persist only a redacted category
            log.exception("unexpected monitor poll failure for %s", monitor.get("id"))
            error = MonitorPollError("poll_error", "Monitor polling failed unexpectedly.", retryable=True)
            return await _finish_failure(monitor, run, error, now=now)
    finally:
        await _release_monitor(str(monitor["id"]), token)


async def _recover_stale_runs(now: datetime) -> int:
    runs = await reflect_table("monitor_runs")
    stale_before = now - timedelta(seconds=_RUNNING_STALE_SECONDS)
    async with engine.begin() as conn:
        result = await conn.execute(
            update(runs)
            .where(runs.c.status == "running", runs.c.started_at < stale_before)
            .values(
                status="retry",
                next_attempt_at=now,
                error_code="interrupted",
                error_summary="The previous poll was interrupted and will resume.",
            )
        )
    return int(result.rowcount or 0)


async def run_monitor_now(
    monitor_id: str,
    organization_id: str,
    *,
    actor_id: str,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    now = _now()
    claim = await _claim_monitor(
        monitor_id, organization_id, now=now, require_due=False
    )
    if claim is None:
        raise MonitorPollError("already_running", "This monitor already has an active poll.", retryable=True)
    monitor, token = claim
    result = await _execute_claimed(
        monitor,
        token,
        trigger_source="manual",
        run_key=f"manual:{idempotency_key or uuid.uuid4().hex}",
        now=now,
    )
    await audit.log(
        "monitor_manual_run",
        actor_id,
        "monitors.run",
        organization_id=organization_id,
        resource_type="monitors",
        resource_id=monitor_id,
        payload={"run_id": result.get("id") if result else None},
    )
    if result is None:
        raise MonitorPollError("duplicate_run", "This manual run was already accepted.", retryable=False)
    return result


async def run_due_monitors(now: datetime | None = None) -> list[dict[str, Any]]:
    """Poll due definitions once across a multi-replica deployment."""

    from runtime import leases

    now = now or _now()
    token = await leases.acquire_lock(
        "monitor_poll", ttl=settings.monitor_poll_interval_seconds
    )
    if token is None:
        return []
    try:
        await _recover_stale_runs(now)
        monitors = await reflect_table("monitors")
        runs = await reflect_table("monitor_runs")
        async with engine.begin() as conn:
            retry_rows = (
                await conn.execute(
                    select(runs)
                    .where(
                        runs.c.status == "retry",
                        runs.c.next_attempt_at <= now,
                    )
                    .order_by(runs.c.next_attempt_at.asc())
                    .limit(_MAX_POLL_CANDIDATES)
                )
            ).mappings().all()
            due_rows = (
                await conn.execute(
                    select(monitors.c.id, monitors.c.organization_id)
                    .where(
                        monitors.c.status == "active",
                        or_(monitors.c.next_run_at.is_(None), monitors.c.next_run_at <= now),
                        or_(monitors.c.backoff_until.is_(None), monitors.c.backoff_until <= now),
                        or_(monitors.c.lease_expires_at.is_(None), monitors.c.lease_expires_at <= now),
                    )
                    .order_by(monitors.c.next_run_at.asc().nullsfirst())
                    .limit(_MAX_POLL_CANDIDATES)
                )
            ).mappings().all()

        per_org: defaultdict[str, int] = defaultdict(int)
        processed_monitor_ids: set[str] = set()
        outcomes: list[dict[str, Any]] = []
        for retry in retry_rows:
            org_id = str(retry["organization_id"])
            if per_org[org_id] >= settings.monitor_max_runs_per_org_cycle:
                continue
            claim = await _claim_monitor(
                str(retry["monitor_id"]), org_id, now=now, require_due=False
            )
            if claim is None:
                continue
            monitor, lease_token = claim
            outcome = await _execute_claimed(
                monitor,
                lease_token,
                trigger_source=str(retry.get("trigger_source") or "scheduler"),
                retry_run=dict(retry),
                now=now,
            )
            if outcome:
                outcomes.append(outcome)
                per_org[org_id] += 1
                processed_monitor_ids.add(str(retry["monitor_id"]))

        for due in due_rows:
            org_id = str(due["organization_id"])
            if str(due["id"]) in processed_monitor_ids:
                continue
            if per_org[org_id] >= settings.monitor_max_runs_per_org_cycle:
                continue
            claim = await _claim_monitor(
                str(due["id"]), org_id, now=now, require_due=True
            )
            if claim is None:
                continue
            monitor, lease_token = claim
            outcome = await _execute_claimed(
                monitor,
                lease_token,
                trigger_source="scheduler",
                now=now,
            )
            if outcome:
                outcomes.append(outcome)
                per_org[org_id] += 1
        return outcomes
    finally:
        await leases.release_lock("monitor_poll", token)


scheduler.add_job(
    run_due_monitors,
    "interval",
    seconds=settings.monitor_poll_interval_seconds,
    id="monitor-polling",
    coalesce=True,
    max_instances=1,
)
