from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import mimetypes
import re
import secrets
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from sqlalchemy import func, insert, select, text, update

from core import audit
from core.config import settings
from core.content_disarm import inspect_active_content
from core.db import engine, reflect_table
from core.exceptions import ApprovalRequired
from core.file_security import (
    FileScanUnavailable,
    record_file_security_event_if_available,
    require_safe_verdict,
    scan_file_bytes,
)
from core.models import ToolResult
from core.object_storage import get_object, put_object
from core.ssrf import UnsafeURLError, assert_safe_url
from core.tool_installer import ensure_runtime_tool
from core.untrusted_content import scan_untrusted_content
from core.workspace import jailed_path, task_workspace_root_from_args

_TIMEOUT_MS = 20_000
_BROWSERBASE_API_URL = "https://api.browserbase.com/v1"
_BROWSERBASE_RUNNING = {"PENDING", "RUNNING"}
_SAFE_OBJECT_SEGMENT = re.compile(r"[^A-Za-z0-9._-]+")
_MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024
log = logging.getLogger(__name__)
_SENSITIVE_DOMAINS = (
    "bank.",
    "paypal.",
    "stripe.",
    "gmail.",
    "accounts.google.",
    "login.microsoftonline.",
    "okta.",
    "1password.",
)


class BrowserbaseAPIError(RuntimeError):
    def __init__(self, operation: str, status_code: int | None = None) -> None:
        self.operation = operation
        self.status_code = status_code
        suffix = f" ({status_code})" if status_code is not None else ""
        super().__init__(f"Browserbase {operation} failed{suffix}")


@dataclass
class _RuntimeHandle:
    playwright: Any
    browser: Any
    context: Any
    page: Any
    provider: str


class BrowserOperator:
    """Persistent browser-session facade used by broker-routed Phase 9 tools.

    Session metadata is stored in Postgres when the Phase 9 migration is present.
    Unit tests and partially migrated dev environments fall back to an in-process
    store so the connector remains truthfully degraded instead of crashing.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, dict[str, Any]] = {}
        # Browser objects are only a per-process connection cache. Durable
        # identity lives in Postgres and Browserbase, so another replica can
        # reconnect after a restart or load-balancer handoff.
        self._pages: dict[str, _RuntimeHandle] = {}

    async def create_session(
        self,
        *,
        organization_id: str,
        member_id: str,
        task_id: str | None = None,
        consent: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = _now()
        session = {
            "id": str(uuid.uuid4()),
            "organization_id": organization_id,
            "region": settings.region,
            "task_id": task_id,
            "member_id": member_id,
            "status": "active",
            "current_url": None,
            "title": None,
            "screenshot_object_path": None,
            "screenshot_data_url": None,
            "cookies_ref": None,
            "storage_state": {},
            "runtime_provider": self._configured_provider(),
            "remote_session_id": None,
            "remote_context_id": None,
            "takeover_state": "none",
            "takeover_reason": None,
            "takeover_summary": None,
            "consent": consent or {},
            "sensitive_site_approvals": [],
            "downloads": [],
            "history": [],
            "created_at": now,
            "updated_at": now,
            "revoked_at": None,
        }
        await self._save_session(session)
        await self._record_event(session, "browser_session_created", {"consent": session["consent"]})
        return _public_session(session)

    async def execute(self, tool: str, args: dict[str, Any]) -> ToolResult:
        action = tool.split(".", 1)[1]
        org_id = str(args.get("__org_id") or settings.org_id)
        task_id = args.get("__task_id")
        session = await self._load_or_create(args, organization_id=org_id, task_id=task_id)
        async with self._session_action_lock(session):
            # The advisory lock serializes actions across API replicas. Reload
            # after acquiring it so this worker acts on the latest DB state.
            session = await self._load_session(session["id"], org_id)
            return await self._execute_action(tool, action, session, args)

    async def _execute_action(
        self,
        tool: str,
        action: str,
        session: dict[str, Any],
        args: dict[str, Any],
    ) -> ToolResult:
        if session["status"] == "revoked":
            raise ApprovalRequired(tool, "browser session has been revoked")
        if session["status"] == "closed" and action not in {"get_state"}:
            raise ApprovalRequired(tool, "browser session has been closed")
        if _consent_expired(session.get("consent")):
            session.update({"status": "revoked", "revoked_at": _now(), "updated_at": _now()})
            await self._save_session(session)
            await self._close_runtime(session["id"])
            await self._destroy_remote_state(session)
            await self._record_event(session, "browser_session_expired", {})
            raise ApprovalRequired(tool, "browser session consent has expired")
        if action == "navigate":
            return await self._navigate(session, args)
        if action == "login_task":
            return await self._login_task(session, args)
        if action == "click":
            return await self._click(session, args)
        if action == "type":
            return await self._type(session, args)
        if action == "select":
            return await self._select(session, args)
        if action == "scroll":
            return await self._scroll(session, args)
        if action == "wait":
            return await self._wait(session, args)
        if action == "extract":
            return await self._extract(session, args)
        if action == "screenshot":
            return await self._screenshot_result(session, "screenshot")
        if action == "download":
            return await self._download(session, args)
        if action == "upload":
            return await self._upload(session, args)
        if action == "read_dom":
            return await self._read_dom(session, args)
        if action == "get_state":
            return ToolResult(data={"session": _public_session(session)}, summary="Browser session state")
        if action == "close":
            closed = await self.close_session(session["id"], organization_id=session["organization_id"])
            return ToolResult(data={"session": closed}, summary="Browser session closed")
        if action == "request_takeover":
            updated = await self.request_takeover(
                session["id"],
                organization_id=session["organization_id"],
                reason=str(args.get("reason") or "User input required"),
            )
            return ToolResult(data={"session": updated}, summary="Browser takeover requested")
        raise ValueError(f"Unknown browser operator tool: {tool}")

    def _configured_provider(self) -> str:
        if settings.browserbase_operator_enabled:
            return "browserbase"
        if settings.is_production:
            # Startup validation normally catches this. Keep the runtime guard
            # too so tests/partial imports cannot silently run local Chromium in
            # a production process.
            raise RuntimeError("Production browser operator requires Browserbase")
        return "local"

    async def request_takeover(self, session_id: str, *, organization_id: str, reason: str) -> dict[str, Any]:
        session = await self._load_session(session_id, organization_id)
        session.update(
            {
                "takeover_state": "requested",
                "takeover_reason": reason,
                "updated_at": _now(),
            }
        )
        await self._save_session(session)
        await self._record_event(session, "browser_takeover_requested", {"reason": reason})
        public = _public_session(session)
        if session.get("runtime_provider") == "browserbase" and session.get("remote_session_id"):
            public["live_view_url"] = await self._browserbase_client().live_view_url(
                str(session["remote_session_id"])
            )
        return public

    async def live_view(self, session_id: str, *, organization_id: str) -> dict[str, Any]:
        """Return a short-lived Browserbase debugger URL after tenant checks.

        The URL is never written to Postgres, logs, history, or audit payloads.
        """

        session = await self._load_session(session_id, organization_id)
        if session.get("runtime_provider") != "browserbase":
            raise RuntimeError("Live takeover is only available for Browserbase sessions")
        await self._runtime_page(session)
        remote_session_id = str(session.get("remote_session_id") or "")
        if not remote_session_id:
            raise RuntimeError("Browserbase session is not ready")
        return {
            "session_id": session["id"],
            "live_view_url": await self._browserbase_client().live_view_url(remote_session_id),
        }

    async def screenshot_object(
        self,
        session_id: str,
        *,
        organization_id: str,
    ) -> tuple[bytes, str]:
        session = await self._load_session(session_id, organization_id)
        object_path = str(session.get("screenshot_object_path") or "")
        if not object_path:
            raise KeyError("screenshot")
        return await get_object(object_path), "image/png"

    async def download_object(
        self,
        session_id: str,
        index: int,
        *,
        organization_id: str,
    ) -> tuple[bytes, dict[str, Any]]:
        session = await self._load_session(session_id, organization_id)
        downloads = list(session.get("downloads") or [])
        if index < 0 or index >= len(downloads):
            raise KeyError("download")
        record = dict(downloads[index])
        if settings.malware_scan_required and record.get("malware_scan_status") != "clean":
            raise KeyError("download")
        object_path = str(record.get("object_path") or "")
        if not object_path:
            raise KeyError("download")
        return await get_object(object_path), record

    async def hand_back(
        self,
        session_id: str,
        *,
        organization_id: str,
        member_id: str,
        summary: str,
    ) -> dict[str, Any]:
        session = await self._load_session(session_id, organization_id)
        session.update(
            {
                "takeover_state": "released",
                "takeover_summary": summary,
                "updated_at": _now(),
            }
        )
        await self._save_session(session)
        await self._record_event(session, "browser_takeover_released", {"member_id": member_id, "summary": summary})
        return _public_session(session)

    async def approve_sensitive_site(
        self,
        session_id: str,
        *,
        organization_id: str,
        member_id: str,
        domain: str,
        approval_id: str | None = None,
    ) -> dict[str, Any]:
        session = await self._load_session(session_id, organization_id)
        approvals = list(session.get("sensitive_site_approvals") or [])
        approvals.append(
            {
                "domain": domain,
                "approval_id": approval_id,
                "approved_by": member_id,
                "approved_at": _stamp(),
            }
        )
        session.update({"sensitive_site_approvals": approvals, "updated_at": _now()})
        await self._save_session(session)
        await self._record_event(session, "browser_sensitive_site_approved", {"domain": domain, "approval_id": approval_id})
        return _public_session(session)

    async def revoke_session(
        self,
        session_id: str,
        *,
        organization_id: str,
        member_id: str,
        reason: str,
    ) -> dict[str, Any]:
        session = await self._load_session(session_id, organization_id)
        session.update({"status": "revoked", "revoked_at": _now(), "updated_at": _now()})
        await self._save_session(session)
        await self._close_runtime(session_id)
        await self._destroy_remote_state(session)
        await self._record_event(session, "browser_session_revoked", {"member_id": member_id, "reason": reason})
        return _public_session(session)

    async def close_session(self, session_id: str, *, organization_id: str) -> dict[str, Any]:
        session = await self._load_session(session_id, organization_id)
        session.update({"status": "closed", "updated_at": _now()})
        await self._save_session(session)
        await self._close_runtime(session_id)
        await self._destroy_remote_state(session)
        await self._record_event(session, "browser_session_closed", {})
        return _public_session(session)

    async def list_sessions(
        self,
        *,
        organization_id: str,
        task_id: str | None = None,
        member_id: str | None = None,
    ) -> list[dict[str, Any]]:
        try:
            table = await reflect_table("browser_sessions")
            stmt = select(table).where(table.c.organization_id == organization_id)
            if task_id:
                stmt = stmt.where(table.c.task_id == task_id)
            if member_id:
                stmt = stmt.where(table.c.member_id == member_id)
            stmt = stmt.order_by(table.c.updated_at.desc())
            async with engine.begin() as conn:
                rows = (await conn.execute(stmt)).mappings().all()
            return [_public_session(_coerce_session(dict(row))) for row in rows]
        except Exception:
            if settings.is_production:
                raise
            sessions = [
                _public_session(session)
                for session in self._sessions.values()
                if session["organization_id"] == organization_id
                and (task_id is None or session.get("task_id") == task_id)
                and (member_id is None or str(session.get("member_id")) == member_id)
            ]
            return sorted(sessions, key=lambda item: str(item.get("updated_at") or ""), reverse=True)

    async def _load_or_create(self, args: dict[str, Any], *, organization_id: str, task_id: str | None) -> dict[str, Any]:
        session_id = args.get("session_id")
        if session_id:
            return await self._load_session(str(session_id), organization_id)
        return await self.create_session(
            organization_id=organization_id,
            member_id=str(args.get("__member_id") or "chronos"),
            task_id=str(task_id) if task_id else None,
            consent=args.get("consent") if isinstance(args.get("consent"), dict) else {},
        )

    async def _navigate(self, session: dict[str, Any], args: dict[str, Any]) -> ToolResult:
        url = str(args.get("url") or "")
        if not url:
            raise ValueError("browser.navigate requires 'url'")
        self._enforce_url_consent(session, url)
        try:
            assert_safe_url(url)
        except UnsafeURLError as exc:
            raise ValueError(f"browser.navigate rejected unsafe URL: {exc}") from exc
        page = await self._runtime_page(session)
        await self._install_network_guard(page, session)
        try:
            await page.goto(url, wait_until="domcontentloaded")
            title = await page.title()
        except Exception:
            title = url
        observed_url = str(getattr(page, "url", None) or "")
        current_url = (
            observed_url
            if urlparse(observed_url).scheme in {"http", "https"}
            else url
        )
        try:
            assert_safe_url(current_url)
        except UnsafeURLError as exc:
            raise ValueError(f"browser.navigate reached unsafe URL after navigation: {exc}") from exc
        self._enforce_url_consent(session, current_url)
        session.update({"current_url": current_url, "title": title, "status": "active"})
        await self._capture_state(session, "navigate")
        await self._record_action(session, "navigate", {"current_url": current_url})
        return ToolResult(data={"session": _public_session(session)}, summary=f"Browser navigated to {current_url}")

    async def _login_task(self, session: dict[str, Any], args: dict[str, Any]) -> ToolResult:
        login_url = str(args.get("login_url") or args.get("url") or "")
        task = str(args.get("task") or args.get("goal") or "").strip()
        if not login_url:
            raise ValueError("browser.login_task requires 'login_url'")
        if not task:
            raise ValueError("browser.login_task requires 'task'")

        consent = session.get("consent") or {}
        if not consent.get("allowed_domains"):
            consent = {
                **consent,
                "purpose": consent.get("purpose") or f"Log in and complete: {task[:160]}",
                "allowed_domains": [_domain(login_url)],
            }
            session["consent"] = consent

        navigate_result = await self._navigate(session, {"url": login_url})
        session = await self._load_session(navigate_result.data["session"]["id"], session["organization_id"])
        reason = (
            "Login required. Enter credentials, complete MFA or CAPTCHA if present, "
            "then hand the browser session back so Chronos can continue the requested task."
        )
        session.update(
            {
                "takeover_state": "requested",
                "takeover_reason": reason,
                "updated_at": _now(),
            }
        )
        await self._save_session(session)
        await self._record_action(session, "login_task", {"login_url": login_url, "task": task[:500], "takeover_reason": reason})
        return ToolResult(
            data={
                "session": _public_session(session),
                "next_step": "user_takeover_required",
                "resume_instructions": (
                    "After the user completes login and hands back the session, continue with "
                    "browser.get_state, browser.read_dom, browser.click, browser.type, "
                    "browser.extract, browser.download, or browser.upload as needed."
                ),
            },
            summary="Browser login task started; user takeover requested for credentials/MFA",
        )

    async def _click(self, session: dict[str, Any], args: dict[str, Any]) -> ToolResult:
        selector = str(args.get("selector") or "")
        if not selector:
            raise ValueError("browser.click requires 'selector'")
        page = await self._runtime_page(session)
        await page.click(selector)
        await self._capture_state(session, "click")
        await self._record_action(session, "click", {"selector": selector})
        return ToolResult(data={"session": _public_session(session)}, summary=f"Browser clicked {selector}")

    async def _type(self, session: dict[str, Any], args: dict[str, Any]) -> ToolResult:
        selector = str(args.get("selector") or "")
        text = str(args.get("text") or "")
        if not selector:
            raise ValueError("browser.type requires 'selector'")
        page = await self._runtime_page(session)
        await page.fill(selector, text)
        await self._capture_state(session, "type")
        await self._record_action(session, "type", {"selector": selector, "text_length": len(text)})
        return ToolResult(data={"session": _public_session(session)}, summary=f"Browser typed into {selector}")

    async def _select(self, session: dict[str, Any], args: dict[str, Any]) -> ToolResult:
        selector = str(args.get("selector") or "")
        value = str(args.get("value") or "")
        page = await self._runtime_page(session)
        await page.select_option(selector, value)
        await self._capture_state(session, "select")
        await self._record_action(session, "select", {"selector": selector, "value": value})
        return ToolResult(data={"session": _public_session(session)}, summary=f"Browser selected {value}")

    async def _scroll(self, session: dict[str, Any], args: dict[str, Any]) -> ToolResult:
        x = int(args.get("x") or 0)
        y = int(args.get("y") or 700)
        page = await self._runtime_page(session)
        await page.mouse.wheel(x, y)
        await self._capture_state(session, "scroll")
        await self._record_action(session, "scroll", {"x": x, "y": y})
        return ToolResult(data={"session": _public_session(session)}, summary="Browser scrolled")

    async def _wait(self, session: dict[str, Any], args: dict[str, Any]) -> ToolResult:
        selector = args.get("selector")
        milliseconds = int(args.get("milliseconds") or 1000)
        page = await self._runtime_page(session)
        if selector:
            await page.wait_for_selector(str(selector), timeout=milliseconds)
        else:
            await page.wait_for_timeout(milliseconds)
        await self._capture_state(session, "wait")
        await self._record_action(session, "wait", {"selector": selector, "milliseconds": milliseconds})
        return ToolResult(data={"session": _public_session(session)}, summary="Browser wait complete")

    async def _extract(self, session: dict[str, Any], args: dict[str, Any]) -> ToolResult:
        selector = str(args.get("selector") or "body")
        page = await self._runtime_page(session)
        text = await page.locator(selector).inner_text(timeout=int(args.get("timeout_ms") or 5000))
        scan = scan_untrusted_content(text[:10_000], source=f"browser:{session.get('current_url') or selector}")
        await self._record_action(session, "extract", {"selector": selector, "text_length": len(text), "untrusted_content": scan})
        return ToolResult(
            data={"session": _public_session(session), "text": text[:10_000], "truncated": len(text) > 10_000, "untrusted_content": scan},
            summary=f"Browser extracted {min(len(text), 10_000)} chars",
        )

    async def _screenshot_result(self, session: dict[str, Any], label: str) -> ToolResult:
        await self._capture_state(session, label)
        await self._record_action(session, "screenshot", {"screenshot_object_path": session.get("screenshot_object_path")})
        return ToolResult(data={"session": _public_session(session)}, summary="Browser screenshot captured")

    async def _download(self, session: dict[str, Any], args: dict[str, Any]) -> ToolResult:
        selector = str(args.get("selector") or "")
        if not selector:
            raise ValueError("browser.download requires 'selector'")
        page = await self._runtime_page(session)
        async with page.expect_download() as download_info:
            await page.click(selector)
        download = await download_info.value
        filename = Path(str(download.suggested_filename or "download.bin")).name[:255]
        local_path = str(await download.path() or "")
        if not local_path:
            raise RuntimeError("Browser download did not produce a readable file")
        file_path = Path(local_path)
        size = await asyncio.to_thread(lambda: file_path.stat().st_size)
        if size > _MAX_DOWNLOAD_BYTES:
            raise ValueError("Browser download exceeds the 50 MiB safety limit")
        body = await asyncio.to_thread(file_path.read_bytes)
        suffix = file_path.suffix.lower().lstrip(".") or "bin"
        object_path = self._object_path(session, "downloads", filename, suffix)
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        scan = await scan_file_bytes(body)
        try:
            require_safe_verdict(scan)
        except ValueError as exc:
            event_id = await record_file_security_event_if_available(
                scan,
                organization_id=session["organization_id"],
                source="browser_download",
                source_ref=session["id"],
                filename=filename,
                mime_type=content_type,
                created_by=str(session.get("member_id") or "") or None,
                content_disarm_status="not_run",
            )
            await self._record_action(
                session,
                "download_rejected_malware",
                {"filename": filename, "file_security_event_id": event_id},
            )
            raise ValueError("Browser download blocked because it contains malware") from exc
        except FileScanUnavailable as exc:
            event_id = await record_file_security_event_if_available(
                scan,
                organization_id=session["organization_id"],
                source="browser_download",
                source_ref=session["id"],
                filename=filename,
                mime_type=content_type,
                created_by=str(session.get("member_id") or "") or None,
                content_disarm_status="not_run",
            )
            await self._record_action(
                session,
                "download_scan_unavailable",
                {"filename": filename, "file_security_event_id": event_id},
            )
            raise RuntimeError("Browser download scanning is temporarily unavailable") from exc
        disarm = inspect_active_content(body, filename=filename, mime_type=content_type)
        if disarm.status != "safe":
            event_id = await record_file_security_event_if_available(
                scan,
                organization_id=session["organization_id"],
                source="browser_download",
                source_ref=session["id"],
                filename=filename,
                mime_type=content_type,
                created_by=str(session.get("member_id") or "") or None,
                content_disarm_status=disarm.status,
                content_disarm_reason=disarm.reason,
            )
            await self._record_action(
                session,
                "download_rejected_active_content",
                {
                    "filename": filename,
                    "file_security_event_id": event_id,
                    "reason": disarm.reason,
                },
            )
            raise ValueError("Browser download blocked because it contains active content")
        await put_object(object_path, body, content_type)
        record = {
            "filename": filename,
            "created_at": _stamp(),
            "object_path": object_path,
            "content_type": content_type,
            "size_bytes": size,
            "malware_scan_status": scan.verdict,
            "malware_scan_engine": scan.engine,
            "malware_scan_engine_version": scan.engine_version,
            "malware_scanned_at": scan.scanned_at.isoformat(),
        }
        event_id = await record_file_security_event_if_available(
            scan,
            organization_id=session["organization_id"],
            source="browser_download",
            source_ref=session["id"],
            filename=filename,
            mime_type=content_type,
            created_by=str(session.get("member_id") or "") or None,
            content_disarm_status="safe",
        )
        record["file_security_event_id"] = event_id
        session["downloads"] = list(session.get("downloads") or []) + [record]
        await self._save_session(session)
        await self._record_action(session, "download", record)
        return ToolResult(
            data={
                "session": _public_session(session),
                "download": _public_download(record, session["id"], len(session["downloads"]) - 1),
            },
            summary=f"Browser downloaded {record['filename']}",
        )

    async def _upload(self, session: dict[str, Any], args: dict[str, Any]) -> ToolResult:
        selector = str(args.get("selector") or "")
        requested_path = str(args.get("path") or "")
        if not requested_path:
            raise ValueError("browser.upload requires 'path'")
        if Path(requested_path).is_absolute():
            raise ValueError("browser.upload path must be relative to the task workspace")
        root = task_workspace_root_from_args(args)
        path = str(jailed_path(root, requested_path))
        file_path = Path(path)
        size = await asyncio.to_thread(lambda: file_path.stat().st_size)
        if size > _MAX_DOWNLOAD_BYTES:
            raise ValueError("Browser upload exceeds the 50 MiB safety limit")
        body = await asyncio.to_thread(file_path.read_bytes)
        content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        scan = await scan_file_bytes(body)
        try:
            require_safe_verdict(scan)
        except ValueError as exc:
            event_id = await record_file_security_event_if_available(
                scan,
                organization_id=session["organization_id"],
                source="browser_upload",
                source_ref=session["id"],
                filename=file_path.name,
                mime_type=content_type,
                created_by=str(session.get("member_id") or "") or None,
                content_disarm_status="not_run",
            )
            await self._record_action(
                session,
                "upload_rejected_malware",
                {"filename": file_path.name, "file_security_event_id": event_id},
            )
            raise ValueError("Browser upload blocked because it contains malware") from exc
        except FileScanUnavailable as exc:
            event_id = await record_file_security_event_if_available(
                scan,
                organization_id=session["organization_id"],
                source="browser_upload",
                source_ref=session["id"],
                filename=file_path.name,
                mime_type=content_type,
                created_by=str(session.get("member_id") or "") or None,
                content_disarm_status="not_run",
            )
            await self._record_action(
                session,
                "upload_scan_unavailable",
                {"filename": file_path.name, "file_security_event_id": event_id},
            )
            raise RuntimeError("Browser upload scanning is temporarily unavailable") from exc
        disarm = inspect_active_content(body, filename=file_path.name, mime_type=content_type)
        if disarm.status != "safe":
            event_id = await record_file_security_event_if_available(
                scan,
                organization_id=session["organization_id"],
                source="browser_upload",
                source_ref=session["id"],
                filename=file_path.name,
                mime_type=content_type,
                created_by=str(session.get("member_id") or "") or None,
                content_disarm_status=disarm.status,
                content_disarm_reason=disarm.reason,
            )
            await self._record_action(
                session,
                "upload_rejected_active_content",
                {
                    "filename": file_path.name,
                    "file_security_event_id": event_id,
                    "reason": disarm.reason,
                },
            )
            raise ValueError("Browser upload blocked because it contains active content")
        event_id = await record_file_security_event_if_available(
            scan,
            organization_id=session["organization_id"],
            source="browser_upload",
            source_ref=session["id"],
            filename=file_path.name,
            mime_type=content_type,
            created_by=str(session.get("member_id") or "") or None,
            content_disarm_status="safe",
        )
        page = await self._runtime_page(session)
        await page.set_input_files(selector, path)
        await self._capture_state(session, "upload")
        await self._record_action(
            session,
            "upload",
            {
                "selector": selector,
                "filename": file_path.name,
                "size_bytes": size,
                "file_security_event_id": event_id,
            },
        )
        return ToolResult(
            data={"session": _public_session(session)},
            summary=f"Browser uploaded {file_path.name}",
        )

    async def _read_dom(self, session: dict[str, Any], args: dict[str, Any]) -> ToolResult:
        selector = str(args.get("selector") or "body")
        page = await self._runtime_page(session)
        html = await page.locator(selector).evaluate("el => el.outerHTML")
        scan = scan_untrusted_content(html[:10_000], source=f"browser-dom:{session.get('current_url') or selector}")
        await self._record_action(session, "read_dom", {"selector": selector, "html_length": len(html), "untrusted_content": scan})
        return ToolResult(
            data={"session": _public_session(session), "html": html[:10_000], "truncated": len(html) > 10_000, "untrusted_content": scan},
            summary=f"Browser DOM read {min(len(html), 10_000)} chars",
        )

    async def _runtime_page(self, session: dict[str, Any]) -> Any:
        cached = self._pages.get(session["id"])
        if cached:
            return cached.page
        provider = str(session.get("runtime_provider") or self._configured_provider())
        session["runtime_provider"] = provider
        if provider == "browserbase":
            return await self._launch_browserbase_page(session)
        if settings.is_production:
            raise RuntimeError("Process-local browser runtime is forbidden in production")

        playwright_api = await self._playwright_api_or_none(session)
        if playwright_api is None:
            page = _MetadataOnlyPage(session)
            self._pages[session["id"]] = _RuntimeHandle(
                _NoopRuntime(), _NoopRuntime(), _NoopRuntime(), page, "degraded"
            )
            session["status"] = "degraded"
            await self._save_session(session)
            return page

        try:
            return await self._launch_page(session, playwright_api)
        except Exception as exc:
            if not _looks_like_missing_chromium(exc):
                raise
            install = await ensure_runtime_tool(
                "playwright.chromium",
                organization_id=session["organization_id"],
                reason="browser runtime missing chromium",
            )
            await self._record_event(
                session,
                "browser_runtime_tool_install",
                {
                    "tool": "playwright.chromium",
                    "status": install.status,
                    "returncode": install.returncode,
                    "reason": install.reason,
                },
            )
            if install.status not in {"installed", "already_installed"}:
                page = _MetadataOnlyPage(session)
                self._pages[session["id"]] = _RuntimeHandle(
                    _NoopRuntime(), _NoopRuntime(), _NoopRuntime(), page, "degraded"
                )
                session["status"] = "degraded"
                await self._save_session(session)
                return page
            return await self._launch_page(session, playwright_api)

    async def _launch_browserbase_page(self, session: dict[str, Any]) -> Any:
        if not settings.browserbase_api_key.strip() or not settings.browserbase_project_id.strip():
            raise RuntimeError(
                "Browserbase operator requires BROWSERBASE_API_KEY and BROWSERBASE_PROJECT_ID"
            )
        async_playwright = await self._playwright_api_or_none(session)
        if async_playwright is None:
            raise RuntimeError("Playwright package is required to connect to Browserbase")

        client = self._browserbase_client()
        context_id = str(session.get("remote_context_id") or "")
        if not context_id:
            context_id = await client.create_context()
            session["remote_context_id"] = context_id
            await self._save_session(session)
            await self._record_event(session, "browser_remote_context_created", {})

        remote: dict[str, Any] | None = None
        remote_id = str(session.get("remote_session_id") or "")
        if remote_id:
            try:
                candidate = await client.get_session(remote_id)
                if str(candidate.get("status") or "").upper() in _BROWSERBASE_RUNNING:
                    remote = candidate
            except BrowserbaseAPIError as exc:
                if exc.status_code != 404:
                    raise

        rehydrated = remote is not None
        if remote is None:
            remote = await client.create_session(
                context_id=context_id,
                chronos_session_id=str(session["id"]),
                organization_id=str(session["organization_id"]),
                timeout_seconds=self._remote_timeout_seconds(session),
            )
            session["remote_session_id"] = str(remote["id"])
            await self._save_session(session)

        connect_url = str(remote.get("connectUrl") or "")
        if not connect_url:
            # A PENDING response may not include its websocket immediately.
            remote = await client.get_session(str(session["remote_session_id"]))
            connect_url = str(remote.get("connectUrl") or "")
        if not connect_url:
            raise BrowserbaseAPIError("session connection URL")

        playwright = await async_playwright().start()
        try:
            browser = await playwright.chromium.connect_over_cdp(
                connect_url,
                timeout=_TIMEOUT_MS,
            )
            contexts = browser.contexts
            if not contexts:
                raise RuntimeError("Browserbase session did not expose a default context")
            context = contexts[0]
            pages = context.pages
            page = pages[0] if pages else await context.new_page()
            page.set_default_timeout(_TIMEOUT_MS)
            await self._install_network_guard(page, session)
            self._pages[session["id"]] = _RuntimeHandle(
                playwright, browser, context, page, "browserbase"
            )
            session.update({"runtime_provider": "browserbase", "status": "active"})
            await self._save_session(session)
            await self._record_event(
                session,
                "browser_remote_session_rehydrated" if rehydrated else "browser_remote_session_created",
                {"provider": "browserbase"},
            )
            return page
        except Exception:
            await playwright.stop()
            raise

    def _remote_timeout_seconds(self, session: dict[str, Any]) -> int:
        configured = max(60, min(21_600, settings.browserbase_session_timeout_seconds))
        consent = session.get("consent") or {}
        expires_raw = str(consent.get("expires_at") or "")
        if not expires_raw:
            return configured
        try:
            expires = datetime.fromisoformat(expires_raw.replace("Z", "+00:00"))
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            remaining = int((expires - _now()).total_seconds())
            return max(60, min(configured, remaining))
        except ValueError:
            return configured

    def _browserbase_client(self) -> "_BrowserbaseClient":
        return _BrowserbaseClient(
            api_key=settings.browserbase_api_key,
            project_id=settings.browserbase_project_id,
            region=settings.browserbase_region,
        )

    async def _destroy_remote_state(self, session: dict[str, Any]) -> None:
        if session.get("runtime_provider") != "browserbase":
            return
        client = self._browserbase_client()
        remote_id = str(session.get("remote_session_id") or "")
        context_id = str(session.get("remote_context_id") or "")
        if remote_id:
            await client.release_session(remote_id)
        if context_id:
            # Context deletion is the credential erasure boundary. We clear the
            # DB references only after Browserbase confirms deletion so cleanup
            # can be retried after a transient provider failure.
            await client.delete_context(context_id)
        session.update(
            {
                "remote_session_id": None,
                "remote_context_id": None,
                "cookies_ref": None,
                "storage_state": {},
                "updated_at": _now(),
            }
        )
        await self._save_session(session)

    def _object_path(
        self,
        session: dict[str, Any],
        kind: str,
        label: str,
        extension: str,
    ) -> str:
        safe_label = _SAFE_OBJECT_SEGMENT.sub("-", label).strip("-.")[:80] or kind
        org_hash = hashlib.sha256(str(session["organization_id"]).encode()).hexdigest()[:16]
        return (
            f"browser-sessions/{org_hash}/{session['id']}/{kind}/"
            f"{_stamp().replace(':', '-')}-{safe_label}-{secrets.token_hex(6)}.{extension}"
        )

    @asynccontextmanager
    async def _session_action_lock(self, session: dict[str, Any]):
        if not settings.is_production:
            yield
            return
        lock_key = int.from_bytes(
            hashlib.sha256(str(session["id"]).encode()).digest()[:8],
            byteorder="big",
            signed=True,
        )
        connection = await engine.connect()
        acquired = False
        try:
            acquired = bool(
                (
                    await connection.execute(
                        text("SELECT pg_try_advisory_lock(:lock_key)"),
                        {"lock_key": lock_key},
                    )
                ).scalar_one()
            )
            if not acquired:
                raise RuntimeError(
                    "Browser session is busy on another replica; retry this action"
                )
            yield
        finally:
            if acquired:
                try:
                    await connection.execute(
                        text("SELECT pg_advisory_unlock(:lock_key)"),
                        {"lock_key": lock_key},
                    )
                finally:
                    await connection.close()
            else:
                await connection.close()
    async def _install_network_guard(self, page: Any, session: dict[str, Any]) -> None:
        if getattr(page, "_chronos_network_guard_installed", False):
            return
        route = getattr(page, "route", None)
        if not callable(route):
            setattr(page, "_chronos_network_guard_installed", True)
            return

        async def _guard_request(playwright_route: Any) -> None:
            request = getattr(playwright_route, "request", None)
            request_url = str(getattr(request, "url", "") or "")
            try:
                assert_safe_url(request_url)
                if bool(getattr(request, "is_navigation_request", False)):
                    self._enforce_url_consent(session, request_url)
            except (UnsafeURLError, ApprovalRequired):
                abort = getattr(playwright_route, "abort", None)
                if callable(abort):
                    await abort()
                return
            cont = getattr(playwright_route, "continue_", None)
            if callable(cont):
                await cont()

        await route("**/*", _guard_request)
        setattr(page, "_chronos_network_guard_installed", True)

    async def _playwright_api_or_none(self, session: dict[str, Any]) -> Any | None:
        try:
            from playwright.async_api import async_playwright  # type: ignore[import]
        except ImportError:
            await self._record_event(
                session,
                "browser_runtime_tool_install",
                {"tool": "playwright.chromium", "status": "skipped", "reason": "playwright package is not installed"},
            )
            return None
        return async_playwright

    async def _launch_page(self, session: dict[str, Any], async_playwright: Any) -> Any:
        playwright = await async_playwright().start()
        try:
            # Local Chromium is a development-only fallback. It runs with the
            # browser sandbox intact; production containers use Browserbase and
            # never need --no-sandbox.
            browser = await playwright.chromium.launch(headless=True)
            context_args: dict[str, Any] = {
                "viewport": {"width": 1280, "height": 800},
                "java_script_enabled": True,
                "accept_downloads": True,
            }
            context = await browser.new_context(**context_args)
            page = await context.new_page()
            page.set_default_timeout(_TIMEOUT_MS)
            await self._install_network_guard(page, session)
            self._pages[session["id"]] = _RuntimeHandle(
                playwright, browser, context, page, "local"
            )
            return page
        except Exception:
            try:
                await playwright.stop()
            finally:
                raise

    async def _capture_state(self, session: dict[str, Any], label: str) -> None:
        cached = self._pages.get(session["id"])
        if not cached:
            await self._save_session(session)
            return
        page = cached.page
        observed_url = str(getattr(page, "url", "") or "")
        if urlparse(observed_url).scheme in {"http", "https"}:
            try:
                assert_safe_url(observed_url)
            except UnsafeURLError as exc:
                raise ValueError(f"browser action reached unsafe URL: {exc}") from exc
            self._enforce_url_consent(session, observed_url)
            session["current_url"] = observed_url
        try:
            title = await page.title()
            session["title"] = title
        except Exception:
            pass
        try:
            png = await page.screenshot(full_page=False)
            session["screenshot_data_url"] = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
            object_path = self._object_path(session, "screenshots", label, "png")
            await put_object(object_path, png, "image/png")
            session["screenshot_object_path"] = object_path
        except Exception:
            if settings.is_production:
                raise
            log.warning("Browser screenshot capture/storage failed", exc_info=True)
        session["updated_at"] = _now()
        await self._save_session(session)

    async def _close_runtime(self, session_id: str) -> None:
        cached = self._pages.pop(session_id, None)
        if not cached:
            return
        try:
            if cached.provider == "local":
                await cached.context.close()
            await cached.browser.close()
        finally:
            await cached.playwright.stop()

    def _enforce_url_consent(self, session: dict[str, Any], url: str) -> None:
        domain = _domain(url)
        consent = session.get("consent") or {}
        allowed = {str(item).lower() for item in consent.get("allowed_domains") or []}
        if allowed and domain not in allowed and not any(domain.endswith(f".{item}") for item in allowed):
            raise ApprovalRequired("browser.navigate", f"domain {domain} is outside the consented browser session scope")
        if _is_sensitive(domain) and not _has_sensitive_approval(session, domain):
            raise ApprovalRequired("browser.navigate", f"sensitive site {domain} requires explicit per-task approval")

    async def _load_session(self, session_id: str, organization_id: str) -> dict[str, Any]:
        try:
            table = await reflect_table("browser_sessions")
            async with engine.begin() as conn:
                row = (
                    await conn.execute(
                        select(table).where(table.c.id == session_id, table.c.organization_id == organization_id)
                    )
                ).mappings().first()
            if not row:
                raise KeyError(session_id)
            return _coerce_session(dict(row))
        except Exception:
            if settings.is_production:
                raise
            session = self._sessions.get(session_id)
            if not session or session["organization_id"] != organization_id:
                raise KeyError(session_id)
            return session

    async def _save_session(self, session: dict[str, Any]) -> None:
        session["updated_at"] = session.get("updated_at") or _now()
        self._sessions[session["id"]] = session
        try:
            table = await reflect_table("browser_sessions")
            values = {
                key: session.get(key)
                for key in table.c.keys()
                if key in session
                and key
                not in {
                    "created_at",
                    "updated_at",
                    # Never persist Playwright credentials or inline images.
                    "storage_state",
                    "cookies_ref",
                    "screenshot_data_url",
                }
            }
            if "storage_state" in table.c:
                values["storage_state"] = {}
            if "cookies_ref" in table.c:
                values["cookies_ref"] = None
            if "screenshot_data_url" in table.c:
                values["screenshot_data_url"] = None
            async with engine.begin() as conn:
                existing = (
                    await conn.execute(select(table.c.id).where(table.c.id == session["id"]))
                ).first()
                if existing:
                    await conn.execute(update(table).where(table.c.id == session["id"]).values(**values, updated_at=_now()))
                else:
                    await conn.execute(insert(table).values(**values))
        except Exception:
            if settings.is_production:
                raise
            return

    async def _record_action(self, session: dict[str, Any], action: str, payload: dict[str, Any]) -> None:
        history = list(session.get("history") or [])
        history.append({"action": action, "payload": payload, "created_at": _stamp()})
        session["history"] = history[-100:]
        await self._save_session(session)
        await self._record_event(session, "browser_action", {"action": action, **payload})

    async def _record_event(self, session: dict[str, Any], action: str, payload: dict[str, Any]) -> None:
        task_id = session.get("task_id") or session["id"]
        event_payload = {
            "type": action,
            "task_id": session.get("task_id"),
            "session_id": session["id"],
            **payload,
        }
        try:
            events = await reflect_table("browser_session_events")
            async with engine.begin() as conn:
                seq = (
                    await conn.execute(
                        select(func.coalesce(func.max(events.c.seq), 0)).where(
                            events.c.organization_id == session["organization_id"],
                            events.c.session_id == session["id"],
                        )
                    )
                ).scalar_one()
                await conn.execute(
                    insert(events).values(
                        organization_id=session["organization_id"],
                        region=settings.region,
                        session_id=session["id"],
                        task_id=session.get("task_id"),
                        seq=int(seq) + 1,
                        event_type=action,
                        url=payload.get("current_url") or session.get("current_url"),
                        screenshot_ref=session.get("screenshot_object_path"),
                        payload=event_payload,
                    )
                )
        except Exception:
            pass
        try:
            await audit.log(
                "activity",
                "chronos",
                action,
                organization_id=session["organization_id"],
                resource_type="tasks" if session.get("task_id") else "browser_sessions",
                resource_id=task_id,
                payload=event_payload,
            )
        except Exception:
            pass


class _BrowserbaseClient:
    """Small async client for Browserbase's official Sessions/Contexts APIs.

    Connection URLs and debugger URLs are intentionally kept in local variables
    only. Provider error bodies are never included in exceptions because they
    can contain signed URLs or account details.
    """

    def __init__(self, *, api_key: str, project_id: str, region: str) -> None:
        self.api_key = api_key.strip()
        self.project_id = project_id.strip()
        self.region = region.strip()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        headers = {
            "X-BB-API-Key": self.api_key,
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(20.0)) as client:
                response = await client.request(
                    method,
                    f"{_BROWSERBASE_API_URL}{path}",
                    headers=headers,
                    json=json,
                )
        except httpx.HTTPError as exc:
            raise BrowserbaseAPIError(operation) from exc
        if response.status_code >= 400:
            raise BrowserbaseAPIError(operation, response.status_code)
        if response.status_code == 204 or not response.content:
            return {}
        try:
            payload = response.json()
        except ValueError as exc:
            raise BrowserbaseAPIError(operation, response.status_code) from exc
        if not isinstance(payload, dict):
            raise BrowserbaseAPIError(operation, response.status_code)
        return payload

    async def create_context(self) -> str:
        payload = await self._request(
            "POST",
            "/contexts",
            operation="context creation",
            json={"projectId": self.project_id},
        )
        context_id = str(payload.get("id") or "")
        if not context_id:
            raise BrowserbaseAPIError("context creation")
        return context_id

    async def create_session(
        self,
        *,
        context_id: str,
        chronos_session_id: str,
        organization_id: str,
        timeout_seconds: int,
    ) -> dict[str, Any]:
        tenant_hash = hashlib.sha256(organization_id.encode()).hexdigest()[:16]
        payload = await self._request(
            "POST",
            "/sessions",
            operation="session creation",
            json={
                "projectId": self.project_id,
                "keepAlive": True,
                "timeout": timeout_seconds,
                "region": self.region,
                "browserSettings": {
                    "viewport": {"width": 1280, "height": 800},
                    "context": {"id": context_id, "persist": True},
                },
                "userMetadata": {
                    "chronosSessionId": chronos_session_id,
                    "tenantHash": tenant_hash,
                },
            },
        )
        if not payload.get("id"):
            raise BrowserbaseAPIError("session creation")
        return payload

    async def get_session(self, session_id: str) -> dict[str, Any]:
        return await self._request(
            "GET",
            f"/sessions/{session_id}",
            operation="session lookup",
        )

    async def live_view_url(self, session_id: str) -> str:
        payload = await self._request(
            "GET",
            f"/sessions/{session_id}/debug",
            operation="live view lookup",
        )
        url = str(payload.get("debuggerFullscreenUrl") or payload.get("debuggerUrl") or "")
        parsed = urlparse(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or not (
                parsed.hostname == "browserbase.com"
                or parsed.hostname.endswith(".browserbase.com")
            )
        ):
            raise BrowserbaseAPIError("live view lookup")
        return url

    async def release_session(self, session_id: str) -> None:
        try:
            current = await self.get_session(session_id)
        except BrowserbaseAPIError as exc:
            if exc.status_code == 404:
                return
            raise
        if str(current.get("status") or "").upper() not in _BROWSERBASE_RUNNING:
            return
        await self._request(
            "POST",
            f"/sessions/{session_id}",
            operation="session release",
            json={"status": "REQUEST_RELEASE", "projectId": self.project_id},
        )
        for _ in range(6):
            await asyncio.sleep(0.5)
            try:
                current = await self.get_session(session_id)
            except BrowserbaseAPIError as exc:
                if exc.status_code == 404:
                    return
                raise
            if str(current.get("status") or "").upper() not in _BROWSERBASE_RUNNING:
                return
        raise BrowserbaseAPIError("session release timeout")

    async def delete_context(self, context_id: str) -> None:
        try:
            await self._request(
                "DELETE",
                f"/contexts/{context_id}",
                operation="context deletion",
            )
        except BrowserbaseAPIError as exc:
            if exc.status_code != 404:
                raise


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp() -> str:
    return _now().isoformat()


def _domain(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def _consent_expired(consent: Any) -> bool:
    if not isinstance(consent, dict) or not consent.get("expires_at"):
        return False
    try:
        expires_at = datetime.fromisoformat(str(consent["expires_at"]).replace("Z", "+00:00"))
    except ValueError:
        return True
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at <= datetime.now(timezone.utc)


def _is_sensitive(domain: str) -> bool:
    return any(domain == item.rstrip(".") or item in domain for item in _SENSITIVE_DOMAINS)


def _has_sensitive_approval(session: dict[str, Any], domain: str) -> bool:
    for approval in session.get("sensitive_site_approvals") or []:
        approved = str(approval.get("domain") or "").lower()
        if domain == approved or domain.endswith(f".{approved}"):
            return True
    return False


def _coerce_session(row: dict[str, Any]) -> dict[str, Any]:
    for key in ("storage_state", "consent", "downloads", "history", "sensitive_site_approvals"):
        if row.get(key) is None:
            row[key] = [] if key in {"downloads", "history", "sensitive_site_approvals"} else {}
    # Legacy state can contain cookies/local storage. Never hydrate it into a
    # browser process, even before the purge migration has run.
    row["storage_state"] = {}
    row["cookies_ref"] = None
    row["screenshot_data_url"] = None
    return row


def _public_session(session: dict[str, Any]) -> dict[str, Any]:
    safe = dict(session)
    for key in ("storage_state", "cookies_ref", "remote_session_id", "remote_context_id"):
        safe.pop(key, None)
    if safe.get("screenshot_object_path"):
        safe["screenshot_url"] = f"/browser-sessions/{safe['id']}/screenshot"
    safe["downloads"] = [
        _public_download(record, str(safe["id"]), index)
        for index, record in enumerate(safe.get("downloads") or [])
    ]
    return safe


def _public_download(record: dict[str, Any], session_id: str, index: int) -> dict[str, Any]:
    return {
        key: value
        for key, value in {
            "filename": record.get("filename"),
            "created_at": record.get("created_at"),
            "content_type": record.get("content_type"),
            "size_bytes": record.get("size_bytes"),
            "malware_scan_status": record.get("malware_scan_status"),
            "malware_scanned_at": record.get("malware_scanned_at"),
            "download_url": f"/browser-sessions/{session_id}/downloads/{index}",
        }.items()
        if value is not None
    }


class _NoopRuntime:
    async def close(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def storage_state(self) -> dict[str, Any]:
        return {}


class _NoopMouse:
    async def wheel(self, _x: int, _y: int) -> None:
        return None


class _NoopDownload:
    suggested_filename = "download.bin"

    async def path(self) -> str:
        return ""


class _NoopDownloadContext:
    async def __aenter__(self) -> "_NoopDownloadContext":
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    @property
    async def value(self) -> _NoopDownload:
        return _NoopDownload()


class _MetadataOnlyLocator:
    def __init__(self, session: dict[str, Any], selector: str) -> None:
        self._session = session
        self._selector = selector

    async def inner_text(self, timeout: int = 5000) -> str:
        return f"Browser runtime unavailable; current URL is {self._session.get('current_url') or 'blank'}."

    async def evaluate(self, _script: str) -> str:
        return f"<div data-selector=\"{self._selector}\">Browser runtime unavailable</div>"


class _MetadataOnlyPage:
    def __init__(self, session: dict[str, Any]) -> None:
        self._session = session
        self.mouse = _NoopMouse()

    def set_default_timeout(self, _timeout: int) -> None:
        return None

    async def goto(self, url: str, wait_until: str | None = None) -> None:
        self._session["current_url"] = url

    async def title(self) -> str:
        return str(self._session.get("current_url") or "Browser session")

    async def screenshot(self, full_page: bool = False) -> bytes:
        return b""

    async def click(self, _selector: str) -> None:
        return None

    async def fill(self, _selector: str, _text: str) -> None:
        return None

    async def select_option(self, _selector: str, _value: str) -> None:
        return None

    async def wait_for_selector(self, _selector: str, timeout: int = 1000) -> None:
        return None

    async def wait_for_timeout(self, _milliseconds: int) -> None:
        return None

    def locator(self, selector: str) -> _MetadataOnlyLocator:
        return _MetadataOnlyLocator(self._session, selector)

    def expect_download(self) -> _NoopDownloadContext:
        return _NoopDownloadContext()

    async def set_input_files(self, _selector: str, _path: str) -> None:
        return None


browser_operator = BrowserOperator()


def _looks_like_missing_chromium(exc: Exception) -> bool:
    message = str(exc).lower()
    return (
        "executable doesn't exist" in message
        or "please run the following command" in message and "playwright install" in message
        or "browsertype.launch" in message and "playwright install" in message
    )
