def test_inline_tools_include_start_task_and_exclude_spawn():
    from runtime.tool_registry import INLINE_CHAT_TOOLS, tool_name

    names = {tool_name(t) for t in INLINE_CHAT_TOOLS}
    assert "start_task" in names
    assert "spawn__subagent" not in names
    assert "browser__search" in names


def test_durable_tools_include_spawn_and_exclude_start_task():
    from runtime.tool_registry import ALL_TOOLS, tool_name

    names = {tool_name(t) for t in ALL_TOOLS}
    assert "spawn__subagent" in names
    assert "start_task" not in names
