from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_health_does_not_call_the_model(monkeypatch):
    """The unauthenticated /health probe must never trigger a billed model call.

    A model completion costs money and ties readiness to a third party, so an
    anonymous caller must not be able to drive it. The model probe lives on the
    admin-only /health/deep endpoint instead.
    """
    import litellm

    import main

    async def _fail(*_args, **_kwargs):
        raise AssertionError("/health must not call the model provider")

    monkeypatch.setattr(litellm, "acompletion", _fail)

    result = await main.health()

    assert "model" not in result["checks"]
    assert result["status"] in {"ok", "degraded"}
