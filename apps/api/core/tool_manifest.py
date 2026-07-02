from __future__ import annotations

from typing import Any

from runtime.tool_registry import ALL_TOOLS, SUBAGENT_TOOLS, tool_name


def _required(schema: dict[str, Any]) -> list[str]:
    return list((schema.get("function") or {}).get("parameters", {}).get("required") or [])


def _description(schema: dict[str, Any]) -> str:
    return str((schema.get("function") or {}).get("description") or "").strip()


def _risk_note(name: str) -> str:
    if name.startswith("gmail__draft"):
        return "Creates drafts only; sending is approval-gated."
    if name.startswith("gmail__send"):
        return "Requires explicit approval."
    if name.startswith("fs__write") or name.startswith("code__"):
        return "Runs inside the task workspace and broker limits."
    return "No extra approval unless broker policy requires it."


def available_tool_names(*, sub_agent: bool = False) -> list[str]:
    tools = SUBAGENT_TOOLS if sub_agent else ALL_TOOLS
    return [tool_name(schema) for schema in tools]


def available_tool_schemas(*, sub_agent: bool = False) -> list[dict[str, Any]]:
    return list(SUBAGENT_TOOLS if sub_agent else ALL_TOOLS)


async def generate_tool_manifest(
    *,
    persona_id: str | None = None,
    org_id: str = "default",
    sub_agent: bool = False,
    tools: list[dict[str, Any]] | None = None,
) -> str:
    """Build the tool declaration block from the runtime tool schemas.

    This keeps prompt-visible capabilities aligned with the exact `tools` array
    sent to the model — pass ``tools`` with the resolved (connector-filtered)
    list so the manifest never advertises a tool the model cannot call.
    Provider connection and approval checks still happen in the ToolBroker;
    the manifest is only routing guidance.
    """
    del persona_id, org_id
    blocks = []
    for schema in (tools if tools is not None else available_tool_schemas(sub_agent=sub_agent)):
        name = tool_name(schema)
        params = (schema.get("function") or {}).get("parameters", {}).get("properties", {})
        param_names = ", ".join(params.keys()) or "none"
        required = ", ".join(_required(schema)) or "none"
        blocks.append(
            "\n".join(
                [
                    f"## `{name}`",
                    _description(schema),
                    f"Parameters: {param_names}.",
                    f"Required: {required}.",
                    f"Routing and approval: {_risk_note(name)}",
                ]
            )
        )
    return "# Available Runtime Tools\n" + "\n\n".join(blocks)


async def generate_compact_tool_routing(
    *,
    persona_id: str | None = None,
    org_id: str = "default",
    sub_agent: bool = False,
) -> str:
    """Build compact prompt guidance; native tool schemas remain authoritative."""
    del persona_id, org_id
    families: dict[str, list[str]] = {}
    for schema in available_tool_schemas(sub_agent=sub_agent):
        name = tool_name(schema)
        family, _, action = name.partition("__")
        if not family:
            continue
        actions = families.setdefault(family, [])
        if action and action not in actions and len(actions) < 8:
            actions.append(action)
    if not families:
        return ""
    lines = [
        "# Runtime Tool Routing",
        "Native tool schemas define exact arguments. Use this compact guide only to choose the right family.",
    ]
    for family in sorted(families):
        lines.append(f"- {family}: {', '.join(families[family])}")
    return "\n".join(lines)
