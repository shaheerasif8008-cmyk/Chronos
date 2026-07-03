import pytest


def test_browser_search_direct_navigation_candidates():
    from connectors import browser

    assert browser._direct_url_candidate("scrape and summarize BBC news") == "https://www.bbc.com/news"
    assert browser._direct_url_candidate("summarize example.com/pricing") == "https://example.com/pricing"
    assert browser._direct_url_candidate("https://www.bbc.com/news") == "https://www.bbc.com/news"
    assert browser._direct_url_candidate("latest funding news") is None


@pytest.mark.asyncio
async def test_browser_search_uses_tavily_when_api_key_is_configured(monkeypatch):
    from connectors import browser

    async def fail_new_page():
        raise AssertionError("Tavily search should not open Playwright")

    async def fake_tavily_search(query: str, max_results: int):
        assert query == "latest funding news"
        assert max_results == 2
        return [
            {"title": "Funding one", "url": "https://example.com/one", "content": "First result"},
            {"title": "Funding two", "url": "https://example.com/two", "content": "Second result", "score": 0.8},
        ]

    monkeypatch.setattr(browser.settings, "demo_mode", False)
    monkeypatch.setattr(browser.settings, "tavily_api_key", "tvly-test")
    monkeypatch.setattr(browser, "_new_page", fail_new_page)
    monkeypatch.setattr(browser, "_tavily_search", fake_tavily_search)

    result = await browser.browser_connector._search({"query": "latest funding news", "max_results": 2})

    assert result.summary == "Tavily search 'latest funding news': 2 results"
    assert result.data["tier"] == "live"
    assert result.data["provider"] == "tavily"
    assert result.data["results"] == [
        {"title": "Funding one", "snippet": "First result", "url": "https://example.com/one"},
        {"title": "Funding two", "snippet": "Second result", "url": "https://example.com/two", "score": 0.8},
    ]


@pytest.mark.asyncio
async def test_browser_search_uses_browserbase_when_api_key_is_configured(monkeypatch):
    from connectors import browser

    async def fail_new_page():
        raise AssertionError("Browserbase search should not open local Playwright")

    async def fake_browserbase_search(query: str, max_results: int):
        assert query == "DHA Karachi rent"
        assert max_results == 3
        return [
            {"title": "Listing one", "url": "https://example.com/one", "text": "First result"},
            {"name": "Listing two", "link": "https://example.com/two", "snippet": "Second result"},
        ]

    monkeypatch.setattr(browser.settings, "demo_mode", False)
    monkeypatch.setattr(browser.settings, "tavily_api_key", "")
    monkeypatch.setattr(browser.settings, "browserbase_api_key", "bb-test")
    monkeypatch.setattr(browser, "_new_page", fail_new_page)
    monkeypatch.setattr(browser, "_browserbase_search", fake_browserbase_search)

    result = await browser.browser_connector._search({"query": "DHA Karachi rent", "max_results": 3})

    assert result.summary == "Browserbase search 'DHA Karachi rent': 2 results"
    assert result.data["tier"] == "live"
    assert result.data["provider"] == "browserbase"
    assert result.data["results"] == [
        {"title": "Listing one", "snippet": "First result", "url": "https://example.com/one"},
        {"title": "Listing two", "snippet": "Second result", "url": "https://example.com/two"},
    ]


@pytest.mark.asyncio
async def test_browser_health_prefers_tavily_without_playwright(monkeypatch):
    from core import connector_health

    monkeypatch.setattr(connector_health.settings, "tavily_api_key", "tvly-test")
    monkeypatch.setattr(connector_health, "_module_available", lambda name: False)

    health = await connector_health.check_connectors(refresh=True)

    assert health["browser"]["status"] == "live"
    assert health["browser"]["tier"] == "live"
    assert "Tavily" in health["browser"]["reason"]


@pytest.mark.asyncio
async def test_browser_health_accepts_browserbase_without_playwright(monkeypatch):
    from core import connector_health

    monkeypatch.setattr(connector_health.settings, "tavily_api_key", "")
    monkeypatch.setattr(connector_health.settings, "browserbase_api_key", "bb-test")
    monkeypatch.setattr(connector_health, "_module_available", lambda name: False)

    health = await connector_health.check_connectors(refresh=True)

    assert health["browser"]["status"] == "live"
    assert health["browser"]["tier"] == "live"
    assert "Browserbase" in health["browser"]["reason"]
