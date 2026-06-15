"""Item 2 — live web search must degrade truthfully, never fabricate results."""
import pytest


@pytest.mark.asyncio
async def test_search_returns_empty_when_live_search_unavailable(monkeypatch):
    from connectors import browser

    async def boom():
        raise RuntimeError("playwright not installed")

    monkeypatch.setattr(browser.settings, "demo_mode", False)
    monkeypatch.setattr(browser.settings, "tavily_api_key", "")
    monkeypatch.setattr(browser, "_new_page", boom)

    result = await browser.browser_connector._search({"query": "q4 funding rounds", "max_results": 5})

    # No fabricated rows — the only honest answer to a failed search is "none".
    assert result.data["results"] == []
    assert result.data["tier"] == "unavailable"
    assert result.data["is_unavailable"] is True
    assert "playwright not installed" in result.data["error"]
    assert "UNAVAILABLE" in result.summary


def test_fabricated_fixture_search_helper_is_removed():
    from connectors import browser

    # The placeholder-result generator is gone so it can never be reintroduced
    # into the live failure path.
    assert not hasattr(browser, "_fixture_search_results")
