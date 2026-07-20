"""
Browser connector — Playwright-based, isolated context per task.

Capabilities:
  browser.search           — DuckDuckGo search → structured results
  browser.fetch            — fetch + parse a URL → text content
  browser.extract_contacts — extract name/email/title from a company page

Each call gets a fresh BrowserContext (no cookie/session cross-contamination).
Screenshots are captured after each step and stored in object storage.

Setup: playwright install chromium  (run once after pip install)
"""
from __future__ import annotations

import logging
import re
import secrets
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import httpx

from core.config import settings
from core.models import ToolResult
from core.object_storage import put_object_sync
from core.untrusted_content import scan_untrusted_content

log = logging.getLogger(__name__)


def _assert_fetchable(url: str) -> None:
    """Block SSRF before navigating: model/untrusted content controls these URLs."""
    from core.ssrf import assert_safe_url, UnsafeURLError
    try:
        assert_safe_url(url)
    except UnsafeURLError as exc:
        raise ValueError(f"refusing to fetch unsafe URL: {exc}") from exc


async def _install_network_guard(context) -> None:
    """Re-validate every request the page makes against the SSRF guard.

    The one-time ``_assert_fetchable`` check on the entry URL is not enough:
    Playwright follows HTTP 30x redirects and in-page (meta/JS) navigations
    without re-checking, so a fetched page could redirect the browser to the
    cloud metadata endpoint or a loopback/private host and the response body
    would be read back to the model. Aborting unsafe requests at the context
    level closes redirect- and subresource-based SSRF (mirrors the browser
    operator's guard).
    """
    from core.ssrf import assert_safe_url, UnsafeURLError

    route = getattr(context, "route", None)
    if not callable(route):
        return

    async def _guard_request(playwright_route) -> None:
        request_url = str(getattr(getattr(playwright_route, "request", None), "url", "") or "")
        try:
            assert_safe_url(request_url)
        except UnsafeURLError:
            await playwright_route.abort()
            return
        except Exception:
            # Never fail-open on an unexpected guard error — abort the request.
            await playwright_route.abort()
            return
        await playwright_route.continue_()

    await route("**/*", _guard_request)


_TIMEOUT_MS = 20_000   # 20-second hard page timeout
_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

async def _save_screenshot(page, label: str) -> str | None:
    """Capture screenshot and upload to object storage. Returns the object path, or None on failure."""
    try:
        png = await page.screenshot(full_page=False)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        object_name = f"browser-screenshots/{ts}-{label}-{secrets.token_hex(4)}.png"
        put_object_sync(object_name, png, "image/png")
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
    await _install_network_guard(context)
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

        if settings.tavily_api_key:
            try:
                results = await _tavily_search(query, max_results)
                trimmed = [_normalize_tavily_result(item) for item in results[:max_results]]
                if not trimmed:
                    direct = await _direct_navigation_fallback(query)
                    if direct:
                        return ToolResult(
                            data={
                                "query": query,
                                "results": [direct],
                                "provider": "direct_navigation",
                                "fallback_reason": "Search provider returned zero results, so Chronos navigated directly.",
                            },
                            summary=f"Tavily search '{query}' returned 0 results; direct navigation found {direct['url']}",
                        )
                return ToolResult(
                    data={"query": query, "results": trimmed, "provider": "tavily", "tier": "live"},
                    summary=f"Tavily search '{query}': {len(trimmed)} results",
                )
            except Exception as exc:
                log.warning("Tavily search failed; falling back to browser search: %s", exc)

        if settings.browserbase_api_key:
            try:
                results = await _browserbase_search(query, max_results)
                trimmed = [_normalize_browserbase_result(item) for item in results[:max_results]]
                trimmed = [item for item in trimmed if item["title"] or item["url"] or item["snippet"]]
                if not trimmed:
                    direct = await _direct_navigation_fallback(query)
                    if direct:
                        return ToolResult(
                            data={
                                "query": query,
                                "results": [direct],
                                "provider": "direct_navigation",
                                "fallback_reason": "Search provider returned zero results, so Chronos navigated directly.",
                            },
                            summary=f"Browserbase search '{query}' returned 0 results; direct navigation found {direct['url']}",
                        )
                return ToolResult(
                    data={"query": query, "results": trimmed, "provider": "browserbase", "tier": "live"},
                    summary=f"Browserbase search '{query}': {len(trimmed)} results",
                )
            except Exception as exc:
                log.warning("Browserbase search failed; falling back to browser search: %s", exc)

        playwright = browser = context = None
        try:
            playwright, browser, context, page = await _new_page()
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
            if not trimmed:
                direct = await _direct_navigation_fallback(query)
                if direct:
                    return ToolResult(
                        data={
                            "query": query,
                            "results": [direct],
                            "provider": "direct_navigation",
                            "fallback_reason": "Search indexing returned zero results, so Chronos navigated directly.",
                        },
                        summary=f"Search '{query}' returned 0 results; direct navigation found {direct['url']}",
                    )
            return ToolResult(
                data={"query": query, "results": trimmed},
                summary=f"Search '{query}': {len(trimmed)} results",
            )
        except Exception as exc:
            # Truthful degraded mode: never fabricate research data. Whether the
            # browser failed to launch or the page failed to load, return an
            # explicit empty/unavailable result so the model reports the outage
            # instead of treating placeholder rows as real findings.
            log.warning("Live web search failed for %r: %s", query, exc)
            return ToolResult(
                data={
                    "query": query,
                    "results": [],
                    "tier": "unavailable",
                    "error": str(exc),
                    "is_unavailable": True,
                    "warning": (
                        "Live web search is currently unavailable and returned no results. "
                        "Do not fabricate or guess results — tell the user web search could "
                        "not be completed."
                    ),
                },
                summary=f"LIVE SEARCH UNAVAILABLE — web search could not be completed ({exc}). No results returned.",
            )
        finally:
            if context is not None:
                await context.close()
            if browser is not None:
                await browser.close()
            if playwright is not None:
                await playwright.stop()

    async def _fetch(self, args: dict) -> ToolResult:
        url = args.get("url", "")
        if not url:
            raise ValueError("browser.fetch requires 'url'")
        _assert_fetchable(url)
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
            scan = scan_untrusted_content(excerpt, source=f"browser:{url}")
            summary = f"Fetched {url}: {len(excerpt)} chars"
            if scan["risk"] == "prompt_injection":
                summary = f"UNTRUSTED CONTENT WARNING — {summary}"
            return ToolResult(
                data={
                    "url": url,
                    "title": title,
                    "content": excerpt,
                    "truncated": len(text) > 10_000,
                    "untrusted_content": scan,
                },
                summary=summary,
            )
        finally:
            await context.close()
            await browser.close()
            await playwright.stop()

    async def _extract_contacts(self, args: dict) -> ToolResult:
        url = args.get("url", "")
        if not url:
            raise ValueError("browser.extract_contacts requires 'url'")
        _assert_fetchable(url)
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


def _direct_url_candidate(query: str) -> str | None:
    q = query.strip()
    if not q:
        return None
    if q.startswith(("http://", "https://")):
        return q
    domain_match = re.search(r"\b([a-z0-9-]+(?:\.[a-z0-9-]+)+)(/[^\s]*)?", q, flags=re.I)
    if domain_match:
        return f"https://{domain_match.group(0)}"
    words = re.findall(r"[a-z0-9]+", q.lower())
    if "bbc" in words and "news" in words:
        return "https://www.bbc.com/news"
    return None


async def _direct_navigation_fallback(query: str) -> dict[str, Any] | None:
    url = _direct_url_candidate(query)
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    playwright = browser = context = None
    try:
        playwright, browser, context, page = await _new_page()
        await page.goto(url, wait_until="domcontentloaded")
        await _save_screenshot(page, "direct-navigation")
        title = await page.title()
        text = await page.evaluate("""() => {
            ['script', 'style', 'nav', 'footer', 'aside'].forEach(tag => {
                document.querySelectorAll(tag).forEach(el => el.remove());
            });
            return document.body ? document.body.innerText.trim() : '';
        }""")
        return {
            "title": title or parsed.netloc,
            "snippet": text[:280],
            "url": page.url,
            "source": "direct_navigation",
        }
    except Exception as exc:
        log.warning("Direct navigation fallback failed for %r -> %s: %s", query, url, exc)
        return None
    finally:
        if context is not None:
            await context.close()
        if browser is not None:
            await browser.close()
        if playwright is not None:
            await playwright.stop()


async def _tavily_search(query: str, max_results: int) -> list[dict[str, Any]]:
    if not query:
        raise ValueError("browser.search requires 'query'")
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(
            "https://api.tavily.com/search",
            json={
                "api_key": settings.tavily_api_key,
                "query": query,
                "search_depth": "basic",
                "max_results": max(1, min(max_results, 20)),
                "include_answer": False,
                "include_raw_content": False,
            },
        )
        response.raise_for_status()
        payload = response.json()
    results = payload.get("results") or []
    if not isinstance(results, list):
        return []
    return [item for item in results if isinstance(item, dict)]


async def _browserbase_search(query: str, max_results: int) -> list[dict[str, Any]]:
    if not query:
        raise ValueError("browser.search requires 'query'")
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(
            settings.browserbase_search_url,
            headers={"x-bb-api-key": settings.browserbase_api_key},
            json={
                "query": query,
                "numResults": max(1, min(max_results, 20)),
            },
        )
        response.raise_for_status()
        payload = response.json()
    results = payload.get("results") or payload.get("data") or payload.get("items") or []
    if not isinstance(results, list):
        return []
    return [item for item in results if isinstance(item, dict)]


def _normalize_tavily_result(item: dict[str, Any]) -> dict[str, Any]:
    result = {
        "title": str(item.get("title") or "").strip(),
        "snippet": str(item.get("content") or item.get("snippet") or "").strip(),
        "url": str(item.get("url") or "").strip(),
    }
    if item.get("score") is not None:
        result["score"] = item["score"]
    return result


def _normalize_browserbase_result(item: dict[str, Any]) -> dict[str, Any]:
    result = {
        "title": str(item.get("title") or item.get("name") or "").strip(),
        "snippet": str(
            item.get("text")
            or item.get("snippet")
            or item.get("description")
            or item.get("content")
            or ""
        ).strip(),
        "url": str(item.get("url") or item.get("link") or "").strip(),
    }
    if item.get("source") is not None:
        result["source"] = item["source"]
    return result


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
