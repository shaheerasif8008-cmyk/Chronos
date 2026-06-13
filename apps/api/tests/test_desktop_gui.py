"""Desktop GUI operator — proof harness.

Covers the three things that make autonomous desktop operation real and
governed:

1. The desktop tools are registered for agent + sub-agent loops, and launching
   an app is risk-tiered (always requires a human approval record).
2. Screenshots are fed back into the agent loop as a vision image block, with
   only the latest screenshot retained so history does not bloat.
3. The bridge genuinely drives a virtual desktop (screenshot + input), and
   degrades truthfully when the display tooling is absent.
"""
from uuid import uuid4

import pytest

from connectors.desktop import _tools_available, desktop_connector


def test_desktop_tools_registered_and_open_app_is_risk_tiered():
    from runtime.tool_registry import (
        ALL_TOOLS,
        ALWAYS_APPROVAL_TOOL_NAMES,
        SUBAGENT_TOOLS,
        tool_name,
    )

    expected = {
        "desktop__create_session",
        "desktop__screenshot",
        "desktop__move",
        "desktop__click",
        "desktop__type",
        "desktop__key",
        "desktop__scroll",
        "desktop__open_app",
        "desktop__get_state",
        "desktop__close",
    }
    all_names = {tool_name(t) for t in ALL_TOOLS}
    sub_names = {tool_name(t) for t in SUBAGENT_TOOLS}

    assert expected <= all_names
    assert expected <= sub_names
    # Risk-tiered governance: launching an app must always gate on approval.
    assert "desktop__open_app" in ALWAYS_APPROVAL_TOOL_NAMES


def test_open_app_gates_in_loop_and_broker():
    from core.tool_broker import _ALWAYS_APPROVAL_TOOLS
    from runtime.agent_loop import _needs_approval

    assert _needs_approval("desktop__open_app") is True
    assert "desktop.open_app" in _ALWAYS_APPROVAL_TOOLS


def test_vision_message_injection_keeps_only_latest():
    from runtime.agent_loop import _append_vision_message, _is_injected_vision

    history: list[dict] = [{"role": "user", "content": "operate the desktop"}]
    _append_vision_message(history, "data:image/png;base64,AAA")
    _append_vision_message(history, "data:image/png;base64,BBB")

    vision = [m for m in history if _is_injected_vision(m)]
    assert len(vision) == 1  # old screenshot pruned
    assert vision[0]["content"][1]["image_url"]["url"].endswith("BBB")
    # The non-screenshot turn is untouched.
    assert history[0] == {"role": "user", "content": "operate the desktop"}


@pytest.mark.asyncio
async def test_desktop_bridge_perceives_and_acts_on_real_display():
    available, _reason = _tools_available()
    if not available:
        pytest.skip("Xvfb/xdotool/scrot not present in this runtime")

    org = f"org-desktop-{uuid4()}"
    session = await desktop_connector.create_session(
        organization_id=org, member_id="m1", task_id=None, purpose="gui acceptance"
    )
    sid = session["id"]
    assert session["status"] == "active"

    shot = await desktop_connector.execute("desktop.screenshot", {"session_id": sid, "__org_id": org})
    assert shot.data["screenshot_data_url"].startswith("data:image/png;base64,")
    assert shot.data["width"] == 1280 and shot.data["height"] == 800

    for tool, args in [
        ("desktop.move", {"x": 200, "y": 150}),
        ("desktop.click", {"x": 60, "y": 70, "clicks": 2}),
        ("desktop.type", {"text": "chronos"}),
        ("desktop.key", {"keys": "ctrl+a"}),
        ("desktop.scroll", {"direction": "down", "amount": 2}),
    ]:
        result = await desktop_connector.execute(tool, {"session_id": sid, "__org_id": org, **args})
        assert "unavailable" not in result.summary.lower()

    state = await desktop_connector.execute("desktop.get_state", {"session_id": sid, "__org_id": org})
    assert state.data["session"]["status"] == "active"
    assert len(state.data["session"]["history"]) >= 6

    closed = await desktop_connector.close_session(sid, organization_id=org)
    assert closed["status"] == "closed"


@pytest.mark.asyncio
async def test_open_app_rejects_destructive_command():
    available, _ = _tools_available()
    if not available:
        pytest.skip("Xvfb/xdotool/scrot not present in this runtime")
    org = f"org-desktop-{uuid4()}"
    session = await desktop_connector.create_session(
        organization_id=org, member_id="m1", task_id=None, purpose="safety"
    )
    with pytest.raises(ValueError):
        await desktop_connector.execute(
            "desktop.open_app", {"session_id": session["id"], "__org_id": org, "command": "rm -rf /"}
        )
