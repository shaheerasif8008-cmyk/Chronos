"""
Tool Broker — the single gateway for ALL connector calls.

Every tool call routes through execute(). No direct connector calls. Ever.
Signature is frozen: execute(agent, tool, args) → ToolResult.
"""
import hashlib
import json
import time

from core import audit, permissions
from core.connector_health import connector_tier
from core.config import settings
from core.exceptions import ApprovalRequired, LoopDetected, RateLimitExceeded, SafetyLimitViolation
from core.models import AgentContext, ToolResult
from core.redis import redis_client
from core.settings_store import tool_policy

# Tools that always require a human approval record — regardless of autonomy level.
_ALWAYS_APPROVAL_TOOLS = {
    "twitter.post",
    "linkedin.post",
    "website.publish",
    "gmail.send",   # Phase 1: no approval system yet → always blocked
}

# Hard safety limits enforced regardless of permissions.
_SAFETY_LIMITS: dict[str, dict] = {
    "gmail.send": {"max_recipients": 10},
}
_MAX_DELETE_RECORDS = 5
_MAX_FINANCIAL_AMOUNT = 100.0

# Rate limit: 10 actions per minute per org (fixed 60-second window).
_RATE_LIMIT = 10
# Loop detection: same tool+args ≥ 10 times in 5-minute window.
_LOOP_THRESHOLD = 10


async def _check_rate_limit(org_id: str) -> None:
    window = int(time.time() / 60)
    key = f"rate:{org_id}:{window}"
    count = await redis_client.incr(key)
    if count == 1:
        await redis_client.expire(key, 120)  # 2-min TTL covers window boundary
    if count > _RATE_LIMIT:
        raise RateLimitExceeded(org_id, count, _RATE_LIMIT)


async def _check_loop(org_id: str, tool: str, args_hash: str) -> None:
    key = f"loop:{org_id}:{tool}:{args_hash}"
    count = await redis_client.incr(key)
    if count == 1:
        await redis_client.expire(key, 300)  # 5-minute window
    if count >= _LOOP_THRESHOLD:
        raise LoopDetected(tool, count)


def _check_safety_limits(tool: str, args: dict) -> None:
    if tool == "gmail.send":
        recipients = args.get("to", [])
        if isinstance(recipients, list) and len(recipients) > _SAFETY_LIMITS["gmail.send"]["max_recipients"]:
            raise SafetyLimitViolation(f"gmail.send: {len(recipients)} recipients exceeds limit of 10")

    # Generic delete guard
    if "delete" in tool:
        ids = args.get("ids", args.get("record_ids", []))
        count = args.get("count", len(ids) if isinstance(ids, list) else 0)
        if count > _MAX_DELETE_RECORDS:
            raise SafetyLimitViolation(f"{tool}: deleting {count} records exceeds limit of {_MAX_DELETE_RECORDS}")

    # Financial guard
    if any(t in tool for t in ("finance.", "payment.")):
        amount = float(args.get("amount", 0))
        if amount > _MAX_FINANCIAL_AMOUNT:
            raise SafetyLimitViolation(f"{tool}: amount ${amount} exceeds limit of ${_MAX_FINANCIAL_AMOUNT}")


async def _route(agent: AgentContext, tool: str, args: dict, vault_ref: str, tier: str = "live") -> ToolResult:
    """Route to the correct connector after all checks pass."""
    provider = tool.split(".")[0]
    routed_args = dict(args)
    routed_args["__connector_tier"] = tier
    routed_args["__org_id"] = agent.org_id
    routed_args["__task_id"] = agent.task_id or agent.id

    if provider == "gmail":
        from connectors.gmail import gmail_connector
        return await gmail_connector.execute(tool, routed_args, vault_ref)

    if provider == "browser":
        from connectors.browser import browser_connector
        return await browser_connector.execute(tool, routed_args)

    if provider == "fs":
        from connectors.filesystem import filesystem_connector
        return await filesystem_connector.execute(tool, routed_args)

    if provider == "code":
        from connectors.code import code_connector
        return await code_connector.execute(tool, routed_args)

    if provider == "mcp":
        from connectors.mcp_client import mcp_connector
        return await mcp_connector.execute(tool, routed_args, agent)

    # Unknown provider — fail clearly rather than silently
    raise ValueError(f"No connector registered for provider: {provider}")


class ToolBroker:
    async def execute(self, agent: AgentContext, tool: str, args: dict) -> ToolResult:
        approved_by_gate = bool(args.pop("__approved_by_gate", False))
        args_hash = hashlib.sha256(json.dumps(args, sort_keys=True).encode()).hexdigest()

        # 1. Permission check (seam — always runs)
        await permissions.check(agent.as_member(), f"use_tool:{tool}", agent.workspace_id or "default")

        # 2. Rate limiting (per org, Redis-backed — survives restarts)
        if not (settings.demo_mode and tool == "gmail.draft"):
            await _check_rate_limit(agent.org_id)

        # 3. Loop detection
        await _check_loop(agent.org_id, tool, args_hash)

        # 4. Hard safety limits (no override possible)
        _check_safety_limits(tool, args)

        # 5. Approval gate — check always-approval set first
        if tool in _ALWAYS_APPROVAL_TOOLS and not approved_by_gate:
            raise ApprovalRequired(tool, "tool requires an approval record (none exists in Phase 1)")
        policy = await tool_policy(agent.org_id, tool.split(".")[0])
        if policy.get("enabled") is False:
            raise ApprovalRequired(tool, "tool is disabled in settings")
        if policy.get("approval_required") is True and not approved_by_gate:
            raise ApprovalRequired(tool, "tool requires approval by settings policy")

        # 6. Audit: tool_call before execution
        await audit.log(
            "tool_call",
            agent.id,
            tool,
            payload={"args_hash": args_hash},   # never log raw args — they may contain credentials
        )

        # 7. Resolve the connector tier. Fixture/demo tiers keep tasks usable
        # when external OAuth or browser dependencies are not configured.
        provider = tool.split(".")[0]
        tier = await connector_tier(provider)
        if tier == "live":
            from connectors.registry import get as registry_get

            connector = await registry_get(agent, tool)
            # vault_ref is the only credential identifier that touches logs
            vault_ref = connector.vault_ref
        else:
            vault_ref = tier

        # 8. Execute via connector
        result = await _route(agent, tool, args, vault_ref, tier)

        # 9. Audit: result summary (never log result.data — may contain sensitive content)
        await audit.log(
            "tool_result",
            agent.id,
            tool,
            payload={"summary": result.summary},
        )

        return result


tool_broker = ToolBroker()


async def execute(agent: AgentContext, tool: str, args: dict) -> ToolResult:
    """Public seam function — signature is frozen at (agent, tool, args) → ToolResult."""
    return await tool_broker.execute(agent, tool, args)
