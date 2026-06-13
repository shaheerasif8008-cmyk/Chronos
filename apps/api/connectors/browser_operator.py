from __future__ import annotations

import base64
import secrets
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import func, insert, select, update

from core import audit
from core.config import settings
from core.db import engine, reflect_table
from core.exceptions import ApprovalRequired
from core.models import ToolResult
from core.tool_installer import ensure_runtime_tool
from core.untrusted_content import scan_untrusted_content

_TIMEOUT_MS = 20_000
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


class BrowserOperator:
    """Persistent browser-session facade used by broker-routed Phase 9 tools.

    Session metadata is stored in Postgres when the Phase 9 migration is present.
    Unit tests and partially migrated dev environments fall back to an in-process
    store so the connector remains truthfully degraded instead of crashing.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, dict[str, Any]] = {}
        self._pages: dict[str, tuple[Any, Any, Any, Any]] = {}

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
        if session["status"] == "revoked":
            raise ApprovalRequired(tool, "browser session has been revoked")
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
        return _public_session(session)

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
        await self._record_event(session, "browser_session_revoked", {"member_id": member_id, "reason": reason})
        return _public_session(session)

    async def close_session(self, session_id: str, *, organization_id: str) -> dict[str, Any]:
        session = await self._load_session(session_id, organization_id)
        session.update({"status": "closed", "updated_at": _now()})
        await self._save_session(session)
        await self._close_runtime(session_id)
        await self._record_event(session, "browser_session_closed", {})
        return _public_session(session)

    async def list_sessions(self, *, organization_id: str, task_id: str | None = None) -> list[dict[str, Any]]:
        try:
            table = await reflect_table("browser_sessions")
            stmt = select(table).where(table.c.organization_id == organization_id)
            if task_id:
                stmt = stmt.where(table.c.task_id == task_id)
            stmt = stmt.order_by(table.c.updated_at.desc())
            async with engine.begin() as conn:
                rows = (await conn.execute(stmt)).mappings().all()
            return [_public_session(_coerce_session(dict(row))) for row in rows]
        except Exception:
            sessions = [
                _public_session(session)
                for session in self._sessions.values()
                if session["organization_id"] == organization_id and (task_id is None or session.get("task_id") == task_id)
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
        page = await self._runtime_page(session)
        try:
            await page.goto(url, wait_until="domcontentloaded")
            title = await page.title()
        except Exception:
            title = url
        session.update({"current_url": url, "title": title, "status": "active"})
        await self._capture_state(session, "navigate")
        await self._record_action(session, "navigate", {"current_url": url})
        return ToolResult(data={"session": _public_session(session)}, summary=f"Browser navigated to {url}")

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
        page = await self._runtime_page(session)
        async with page.expect_download() as download_info:
            await page.click(selector)
        download = await download_info.value
        record = {
            "filename": download.suggested_filename,
            "created_at": _stamp(),
            "path": await download.path(),
        }
        session["downloads"] = list(session.get("downloads") or []) + [record]
        await self._save_session(session)
        await self._record_action(session, "download", record)
        return ToolResult(data={"session": _public_session(session), "download": record}, summary=f"Browser downloaded {record['filename']}")

    async def _upload(self, session: dict[str, Any], args: dict[str, Any]) -> ToolResult:
        selector = str(args.get("selector") or "")
        path = str(args.get("path") or "")
        page = await self._runtime_page(session)
        await page.set_input_files(selector, path)
        await self._capture_state(session, "upload")
        await self._record_action(session, "upload", {"selector": selector, "path": path})
        return ToolResult(data={"session": _public_session(session)}, summary=f"Browser uploaded {path}")

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
            return cached[3]
        playwright_api = await self._playwright_api_or_none(session)
        if playwright_api is None:
            page = _MetadataOnlyPage(session)
            self._pages[session["id"]] = (_NoopRuntime(), _NoopRuntime(), _NoopRuntime(), page)
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
                self._pages[session["id"]] = (_NoopRuntime(), _NoopRuntime(), _NoopRuntime(), page)
                session["status"] = "degraded"
                await self._save_session(session)
                return page
            return await self._launch_page(session, playwright_api)

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
            browser = await playwright.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
            context_args: dict[str, Any] = {
                "viewport": {"width": 1280, "height": 800},
                "java_script_enabled": True,
                "accept_downloads": True,
            }
            if session.get("storage_state"):
                context_args["storage_state"] = session["storage_state"]
            context = await browser.new_context(**context_args)
            page = await context.new_page()
            page.set_default_timeout(_TIMEOUT_MS)
            self._pages[session["id"]] = (playwright, browser, context, page)
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
        _, _, context, page = cached
        try:
            title = await page.title()
            session["title"] = title
        except Exception:
            pass
        try:
            png = await page.screenshot(full_page=False)
            session["screenshot_data_url"] = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
            session["screenshot_object_path"] = f"browser-screenshots/{session['id']}-{label}-{secrets.token_hex(4)}.png"
        except Exception:
            pass
        try:
            session["storage_state"] = await context.storage_state()
            session["cookies_ref"] = f"browser-session:{session['id']}:storage_state"
        except Exception:
            pass
        session["updated_at"] = _now()
        await self._save_session(session)

    async def _close_runtime(self, session_id: str) -> None:
        cached = self._pages.pop(session_id, None)
        if not cached:
            return
        playwright, browser, context, _ = cached
        try:
            await context.close()
        finally:
            try:
                await browser.close()
            finally:
                await playwright.stop()

    def _enforce_url_consent(self, session: dict[str, Any], url: str) -> None:
        domain = _domain(url)
        consent = session.get("consent") or {}
        allowed = {str(item).lower() for item in consent.get("allowed_domains") or []}
        if allowed and domain not in allowed and not any(domain.endswith(f".{item}") for item in allowed):
            if not _is_sensitive(domain):
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
                if key in session and key not in {"created_at", "updated_at"}
            }
            async with engine.begin() as conn:
                existing = (
                    await conn.execute(select(table.c.id).where(table.c.id == session["id"]))
                ).first()
                if existing:
                    await conn.execute(update(table).where(table.c.id == session["id"]).values(**values, updated_at=_now()))
                else:
                    await conn.execute(insert(table).values(**values))
        except Exception:
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


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp() -> str:
    return _now().isoformat()


def _domain(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


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
    return row


def _public_session(session: dict[str, Any]) -> dict[str, Any]:
    safe = dict(session)
    safe.pop("storage_state", None)
    return safe


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
