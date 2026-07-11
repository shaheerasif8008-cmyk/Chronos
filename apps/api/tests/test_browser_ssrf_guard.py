"""Regression tests for the browser connector's per-request SSRF guard.

The one-time ``_assert_fetchable`` check on the entry URL does not cover
redirects or in-page navigations. ``_install_network_guard`` re-validates every
request the page issues and aborts ones that resolve to internal/metadata hosts,
closing redirect- and subresource-based SSRF.
"""
import pytest

from connectors.browser import _install_network_guard


class _FakeRequest:
    def __init__(self, url: str) -> None:
        self.url = url


class _FakeRoute:
    def __init__(self, url: str) -> None:
        self.request = _FakeRequest(url)
        self.action: str | None = None

    async def abort(self) -> None:
        self.action = "abort"

    async def continue_(self) -> None:
        self.action = "continue"


class _FakeContext:
    """Captures the route handler so we can drive it directly."""

    def __init__(self) -> None:
        self.handler = None

    async def route(self, pattern: str, handler) -> None:
        self.handler = handler


@pytest.mark.asyncio
async def test_guard_aborts_metadata_and_loopback_requests(monkeypatch):
    # Avoid real DNS: treat these literal hosts as unsafe.
    from core import ssrf

    def fake_assert(url: str) -> str:
        if "169.254.169.254" in url or "127.0.0.1" in url or "localhost" in url:
            raise ssrf.UnsafeURLError("blocked")
        return url

    monkeypatch.setattr("core.ssrf.assert_safe_url", fake_assert)

    ctx = _FakeContext()
    await _install_network_guard(ctx)
    assert ctx.handler is not None

    for blocked in (
        "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
        "http://127.0.0.1:8000/admin",
        "http://localhost/internal",
    ):
        route = _FakeRoute(blocked)
        await ctx.handler(route)
        assert route.action == "abort", blocked


@pytest.mark.asyncio
async def test_guard_allows_public_requests(monkeypatch):
    monkeypatch.setattr("core.ssrf.assert_safe_url", lambda url: url)

    ctx = _FakeContext()
    await _install_network_guard(ctx)

    route = _FakeRoute("https://example.com/page")
    await ctx.handler(route)
    assert route.action == "continue"


@pytest.mark.asyncio
async def test_guard_fails_closed_on_unexpected_error(monkeypatch):
    def boom(url: str):
        raise RuntimeError("resolver exploded")

    monkeypatch.setattr("core.ssrf.assert_safe_url", boom)

    ctx = _FakeContext()
    await _install_network_guard(ctx)

    route = _FakeRoute("https://example.com/page")
    await ctx.handler(route)
    assert route.action == "abort"
