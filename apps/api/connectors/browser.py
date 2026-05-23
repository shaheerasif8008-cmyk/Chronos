"""
Browser connector — Playwright-based, isolated context per task.

Capabilities:
  browser.search           — DuckDuckGo search → structured results
  browser.fetch            — fetch + parse a URL → text content
  browser.extract_contacts — extract name/email/title from a company page

Each call gets a fresh BrowserContext (no cookie/session cross-contamination).
Screenshots are captured after each step and stored in MinIO.

Setup: playwright install chromium  (run once after pip install)
"""
from __future__ import annotations

import logging
import re
import secrets
from datetime import datetime
from typing import Any

from core.config import settings
from core.models import ToolResult

log = logging.getLogger(__name__)

_TIMEOUT_MS = 20_000   # 20-second hard page timeout
_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


async def _save_screenshot(page, label: str) -> str | None:
    """Capture screenshot and upload to MinIO.  Returns the object path, or None on failure."""
    try:
        from minio import Minio  # type: ignore[import]
        import io

        png = await page.screenshot(full_page=False)
        ts = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
        object_name = f"browser-screenshots/{ts}-{label}-{secrets.token_hex(4)}.png"

        client = Minio(
            settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=settings.minio_secure,
        )
        client.put_object(
            settings.minio_bucket,
            object_name,
            io.BytesIO(png),
            length=len(png),
            content_type="image/png",
        )
        return object_name
    except Exception as exc:
        log.warning("Screenshot upload failed (non-fatal): %s", exc)
        return None


async def _new_page():
    """Return a new (context, page) pair — caller must close context."""
    try:
        from playwright.async_api import async_playwright  # type: ignore[import]
    except ImportError as e:
        raise RuntimeError("playwright not installed — run: pip install playwright && playwright install chromium") from e

    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-dev-shm-usage"],
    )
    context = await browser.new_context(
        user_agent=_USER_AGENT,
        viewport={"width": 1280, "height": 800},
        java_script_enabled=True,
    )
    page = await context.new_page()
    page.set_default_timeout(_TIMEOUT_MS)
    return playwright, browser, context, page


class BrowserConnector:
    async def execute(self, tool: str, args: dict[str, Any]) -> ToolResult:
        if tool == "browser.search":
            return await self._search(args)
        if tool == "browser.fetch":
            return await self._fetch(args)
        if tool == "browser.extract_contacts":
            return await self._extract_contacts(args)
        raise ValueError(f"Unknown browser tool: {tool}")

    async def _search(self, args: dict) -> ToolResult:
        query = args.get("query", "")
        max_results = int(args.get("max_results", 10))
        tier = args.pop("__connector_tier", "live")

        if settings.demo_mode or tier in {"demo", "fixture"} or args.get("fixture") == "operator_workflow_proof":
            results = _fixture_leads(max_results)
            return ToolResult(
                data={"query": query, "results": results, "leads": results, "tier": tier},
                summary=f"Fixture search '{query}': {len(results)} leads",
            )

        playwright, browser, context, page = await _new_page()
        try:
            url = f"https://html.duckduckgo.com/html/?q={_url_encode(query)}"
            await page.goto(url, wait_until="domcontentloaded")
            await _save_screenshot(page, "search")

            results = await page.evaluate("""() => {
                const items = document.querySelectorAll('.result__body');
                return Array.from(items).slice(0, 15).map(el => {
                    const titleEl = el.querySelector('.result__title');
                    const snippetEl = el.querySelector('.result__snippet');
                    const linkEl = el.querySelector('.result__url');
                    return {
                        title: titleEl ? titleEl.innerText.trim() : '',
                        snippet: snippetEl ? snippetEl.innerText.trim() : '',
                        url: linkEl ? linkEl.href : '',
                    };
                }).filter(r => r.title);
            }""")

            trimmed = results[:max_results]
            return ToolResult(
                data={"query": query, "results": trimmed},
                summary=f"Search '{query}': {len(trimmed)} results",
            )
        except Exception as exc:
            results = _fixture_search_results(query, max_results)
            return ToolResult(
                data={"query": query, "results": results, "tier": "fixture", "fallback_reason": str(exc)},
                summary=f"Browser search fallback '{query}': {len(results)} fixture results",
            )
        finally:
            await context.close()
            await browser.close()
            await playwright.stop()

    async def _fetch(self, args: dict) -> ToolResult:
        url = args.get("url", "")
        if not url:
            raise ValueError("browser.fetch requires 'url'")
        tier = args.pop("__connector_tier", "live")
        if settings.demo_mode or tier in {"demo", "fixture"}:
            return ToolResult(
                data={"url": url, "title": "Fixture page", "content": "", "truncated": False, "tier": tier},
                summary=f"Fixture fetch {url}: 0 chars",
            )

        playwright, browser, context, page = await _new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded")
            await _save_screenshot(page, "fetch")

            text = await page.evaluate("""() => {
                // Remove noise nodes
                ['script', 'style', 'nav', 'footer', 'aside'].forEach(tag => {
                    document.querySelectorAll(tag).forEach(el => el.remove());
                });
                return document.body ? document.body.innerText.trim() : '';
            }""")

            title = await page.title()
            # Truncate to 10k chars — callers can ask for more specific data via extract_contacts
            excerpt = text[:10_000]
            return ToolResult(
                data={"url": url, "title": title, "content": excerpt, "truncated": len(text) > 10_000},
                summary=f"Fetched {url}: {len(excerpt)} chars",
            )
        finally:
            await context.close()
            await browser.close()
            await playwright.stop()

    async def _extract_contacts(self, args: dict) -> ToolResult:
        url = args.get("url", "")
        if not url:
            raise ValueError("browser.extract_contacts requires 'url'")
        tier = args.pop("__connector_tier", "live")
        if settings.demo_mode or tier in {"demo", "fixture"}:
            return ToolResult(
                data={"url": url, "emails": [], "phones": [], "people": [], "tier": tier},
                summary=f"Fixture contacts {url}: 0 emails, 0 people",
            )

        playwright, browser, context, page = await _new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded")
            await _save_screenshot(page, "contacts")

            text = await page.evaluate("() => document.body ? document.body.innerText : ''")

            emails = list({e.lower() for e in _EMAIL_RE.findall(text)})
            phones = list({p for p in _PHONE_RE.findall(text)})

            # Heuristic: find lines that look like "Name — Title" or "Name, Title"
            people: list[dict] = []
            for m in _PERSON_RE.finditer(text):
                name = m.group(1).strip()
                title = m.group(2).strip()
                if len(name.split()) >= 2 and len(name) < 60:
                    people.append({"name": name, "title": title})

            contacts = {
                "url": url,
                "emails": emails[:20],
                "phones": phones[:10],
                "people": people[:20],
            }
            return ToolResult(
                data=contacts,
                summary=f"Extracted from {url}: {len(emails)} emails, {len(people)} people",
            )
        finally:
            await context.close()
            await browser.close()
            await playwright.stop()


# Regex helpers
_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
_PHONE_RE = re.compile(r"[\+]?[\d\s\-\(\)\.]{10,18}")
_PERSON_RE = re.compile(r"([A-Z][a-z]+(?: [A-Z][a-z]+)+)[,\-–—|]\s*([A-Za-z &/]+)")


def _url_encode(s: str) -> str:
    from urllib.parse import quote_plus
    return quote_plus(s)


browser_connector = BrowserConnector()


def _fixture_leads(max_results: int) -> list[dict[str, Any]]:
    return [
        {
            "title": f"DemoSaaS {i:02d} hiring sales team",
            "snippet": (
                f"Series {'A' if i % 2 else 'B'} B2B SaaS company with "
                f"{75 + (i * 5 % 110)} employees hiring SDRs and AEs."
            ),
            "url": f"https://demosaas{i:02d}.example.com/careers",
            "company": f"DemoSaaS {i:02d}",
            "domain": f"demosaas{i:02d}.example.com",
            "employee_count": 75 + (i * 5 % 110),
            "stage": "Series A" if i % 2 else "Series B",
            "hiring_signal": "Open sales roles listed on careers page.",
            "personalization": "Reference their current sales hiring motion.",
            "score": 8 + (i % 3),
        }
        for i in range(1, 21)
    ][:max_results]


def _fixture_search_results(query: str, max_results: int) -> list[dict[str, Any]]:
    return [
        {
            "title": f"Fixture research result {i}: {query}",
            "snippet": "Live browser search was unavailable, so Chronos recorded a deterministic fallback result for this research step.",
            "url": f"https://example.com/research/{i}",
        }
        for i in range(1, max_results + 1)
    ]
