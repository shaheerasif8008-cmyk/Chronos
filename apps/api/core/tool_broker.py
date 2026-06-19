from __future__ import annotations
"""
Tool Broker — the single gateway for ALL connector calls.

Every tool call routes through execute(). No direct connector calls. Ever.
Signature is frozen: execute(agent, tool, args) → ToolResult.
"""
import hashlib
import json
import time
from typing import Any

from core import audit, autonomy, permissions, risk as risk_pricer, trust
from core.connector_health import connector_tier, degraded_note
from core.config import settings
from core.exceptions import ApprovalRequired, LoopDetected, RateLimitExceeded, SafetyLimitViolation
from core.models import AgentContext, ToolResult
from core.redis import redis_client
from core.settings_store import tool_policy, workspace_autonomy
from core.untrusted_content import scan_untrusted_content

# The hard floor: tools that always require a human approval record — regardless
# of autonomy level, including ``full_auto``. External publish, payments, mass
# email (gmail.send — see RULE 8), and local-machine shell/app launches can never
# be bypassed by a workspace running in full-auto.
_ALWAYS_APPROVAL_TOOLS = {
    "twitter.post",
    "linkedin.post",
    "website.publish",
    "gmail.send",   # Phase 1: no approval system yet → always blocked
    "local_computer.exec",
    "local_computer.open_app",
    "desktop.open_app",  # launching an app into the virtual desktop is risk-tiered
}

# Hard safety limits enforced regardless of permissions.
_SAFETY_LIMITS: dict[str, dict] = {
    "gmail.send": {"max_recipients": 10},
    "image.generate": {"max_count": 4},
}
_MAX_DELETE_RECORDS = 5
_MAX_FINANCIAL_AMOUNT = 100.0

# Rate limit: 10 actions per minute per org (fixed 60-second window).
_RATE_LIMIT = 10
# Loop detection: same tool+args ≥ 10 times in 5-minute window.
_LOOP_THRESHOLD = 10
_WRITE_ACTION_MARKERS = (
    ".draft",
    ".send",
    ".post",
    ".publish",
    ".write",
    ".create",
    ".update",
    ".delete",
    ".upload",
    ".move",
    ".copy",
)
_IDEMPOTENCY_TTL_SECONDS = 60 * 60 * 24
_UNTRUSTED_PROVIDER_PREFIXES = {"browser", "gmail", "mcp"}


def _is_external_write_tool(tool: str) -> bool:
    return tool in _ALWAYS_APPROVAL_TOOLS or any(marker in tool for marker in _WRITE_ACTION_MARKERS)


def _cache_key(org_id: str, tool: str, idempotency_key: str) -> str:
    digest = hashlib.sha256(f"{org_id}:{tool}:{idempotency_key}".encode()).hexdigest()
    return f"idempotency:{digest}"


async def _load_idempotent_result(org_id: str, tool: str, idempotency_key: str | None) -> ToolResult | None:
    if not idempotency_key:
        return None
    raw = await redis_client.get(_cache_key(org_id, tool, idempotency_key))
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None
    if payload.get("tool") != tool:
        return None
    result = payload.get("result") or {}
    if not isinstance(result, dict):
        return None
    return ToolResult(summary=str(result.get("summary") or ""), data=result.get("data") or {})


async def _store_idempotent_result(
    org_id: str,
    tool: str,
    idempotency_key: str | None,
    result: ToolResult,
) -> None:
    if not idempotency_key:
        return
    payload = {
        "tool": tool,
        "result": {"summary": result.summary, "data": result.data},
        "stored_at": int(time.time()),
    }
    await redis_client.set(
        _cache_key(org_id, tool, idempotency_key),
        json.dumps(payload, default=str),
        ex=_IDEMPOTENCY_TTL_SECONDS,
    )


def _check_untrusted_source_policy(tool: str, args: dict[str, Any]) -> None:
    if not args.pop("__triggered_by_untrusted_content", False):
        return
    if _is_external_write_tool(tool):
        raise ApprovalRequired(tool, "untrusted external content cannot trigger write actions without approval")


def _extract_text_fragments(value: Any, fragments: list[str], limit: int = 10) -> None:
    if len(fragments) >= limit:
        return
    if isinstance(value, str) and value.strip():
        fragments.append(value)
        return
    if isinstance(value, dict):
        for nested in value.values():
            _extract_text_fragments(nested, fragments, limit)
            if len(fragments) >= limit:
                return
    elif isinstance(value, list):
        for nested in value:
            _extract_text_fragments(nested, fragments, limit)
            if len(fragments) >= limit:
                return


def _annotate_degraded_result(result: ToolResult, note: str) -> ToolResult:
    """Flag a result as placeholder data so the model doesn't treat it as real.

    The note rides on both the summary (which the model reads inline) and the
    structured ``data`` so downstream code can detect the degraded state."""
    if result.data.get("degraded_connector"):
        return result
    data = dict(result.data)
    data["degraded_connector"] = note
    return ToolResult(
        summary=f"[DEGRADED — placeholder data, not real] {note} :: {result.summary}",
        data=data,
    )


def _mark_untrusted_connector_result(tool: str, result: ToolResult) -> ToolResult:
    provider = tool.split(".")[0]
    if provider not in _UNTRUSTED_PROVIDER_PREFIXES or _is_external_write_tool(tool) or result.data.get("untrusted_content"):
        return result
    fragments: list[str] = []
    _extract_text_fragments(result.data, fragments)
    if not fragments:
        return result
    scan = scan_untrusted_content("\n\n".join(fragments), source=f"{provider}:{tool}")
    data = dict(result.data)
    data["untrusted_content"] = scan
    if scan.get("risk") == "prompt_injection":
        summary = f"UNTRUSTED CONTENT WARNING: {result.summary}"
    else:
        summary = result.summary
    return ToolResult(summary=summary, data=data)


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

    if tool == "image.generate":
        max_count = _SAFETY_LIMITS["image.generate"]["max_count"]
        try:
            count = int(args.get("count", 1))
        except (TypeError, ValueError):
            raise SafetyLimitViolation("image.generate: count must be an integer")
        if not (1 <= count <= max_count):
            raise SafetyLimitViolation(
                f"image.generate: count {count} must be between 1 and {max_count}"
            )

    if tool in {"computer.exec", "computer.install_package", "local_computer.exec"}:
        command = str(args.get("command") or args.get("package") or "").lower()
        risky_markers = ("rm -rf", "mkfs", "diskutil erase", "shutdown", "reboot", ":(){")
        if any(marker in command for marker in risky_markers):
            raise SafetyLimitViolation(f"{tool}: command rejected by safety policy")

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


async def _check_token_budget(org_id: str) -> None:
    """Enforce the per-org daily token budget by checking a Redis counter.

    The counter is incremented by `record_tokens_used` after each model call.
    This function just reads the current total and raises if over budget.
    """
    import datetime

    day = datetime.date.today().isoformat()
    key = f"tokens:{org_id}:{day}"
    raw = await redis_client.get(key)
    used = int(raw) if raw else 0
    if used >= settings.per_org_daily_token_limit:
        raise RateLimitExceeded(org_id, used, settings.per_org_daily_token_limit)


async def record_tokens_used(org_id: str, tokens: int) -> None:
    """Increment the per-org daily token counter. Call after each model completion."""
    import datetime

    day = datetime.date.today().isoformat()
    key = f"tokens:{org_id}:{day}"
    count = await redis_client.incrby(key, tokens)
    if count == tokens:  # first write today — set TTL to 25 hours
        await redis_client.expire(key, 90_000)


async def _route_skill_run_script(agent: AgentContext, args: dict, tier: str) -> ToolResult:
    """Handle skill.run_script — resolve script, guard against path traversal, delegate to data connector."""
    from pathlib import Path
    from skills.registry import SKILLS_ROOT

    skill_id = str(args.get("skill_id") or "")
    script_name = str(args.get("script_name") or "")
    params = args.get("params") or {}

    if not skill_id or not script_name:
        raise ValueError("skill.run_script requires skill_id and script_name")

    skill_dir = (SKILLS_ROOT / skill_id).resolve()
    script_path = (skill_dir / script_name).resolve()

    # Path-traversal guard: script must resolve inside skill_dir
    try:
        script_path.relative_to(skill_dir)
    except ValueError:
        raise ValueError(
            f"skill.run_script: path traversal detected — '{script_name}' escapes skill directory"
        )

    if not script_path.exists():
        raise FileNotFoundError(f"skill.run_script: script not found: {skill_id}/{script_name}")

    # Per-script permission check (RULE 3 — never inline permission logic).
    await permissions.check(agent.as_member(), "skill.run_script", f"skill:{skill_id}/{script_name}")

    script_source = script_path.read_text()

    from connectors.data_analysis import data_analysis_connector
    routed_args = {
        "__connector_tier": tier,
        "__org_id": agent.org_id,
        "__task_id": agent.task_id or agent.id,
        "code": script_source,
        "context": params,
    }
    return await data_analysis_connector.execute("data.run_script", routed_args)


async def _route(agent: AgentContext, tool: str, args: dict, vault_ref: str, tier: str = "live") -> ToolResult:
    """Route to the correct connector after all checks pass."""
    provider = tool.split(".")[0]
    routed_args = dict(args)
    routed_args["__connector_tier"] = tier
    routed_args["__org_id"] = agent.org_id
    routed_args["__task_id"] = agent.task_id or agent.id

    if tool == "skill.run_script":
        return await _route_skill_run_script(agent, args, tier)

    if provider == "gmail":
        from connectors.gmail import gmail_connector
        return await gmail_connector.execute(tool, routed_args, vault_ref)

    if provider == "browser":
        if tool in {"browser.search", "browser.fetch", "browser.extract_contacts"}:
            from connectors.browser import browser_connector
            return await browser_connector.execute(tool, routed_args)
        from connectors.browser_operator import browser_operator
        return await browser_operator.execute(tool, routed_args)

    if provider == "fs":
        from connectors.filesystem import filesystem_connector
        return await filesystem_connector.execute(tool, routed_args)

    if provider == "code":
        from connectors.code import code_connector
        return await code_connector.execute(tool, routed_args)

    if provider == "image":
        from connectors.image_gen import image_gen_connector
        return await image_gen_connector.execute(tool, routed_args)

    if provider == "doc":
        if tool in {"doc.create", "doc.create_slides", "doc.fill_pdf", "doc.render_chart",
                    "doc.detect_fields", "doc.verify_fill"}:
            from connectors.doc_authoring import doc_authoring_connector
            return await doc_authoring_connector.execute(tool, routed_args)
        from parsing.tool import doc_connector
        return await doc_connector.execute(tool, routed_args)

    if provider == "voice":
        from connectors.voice import voice_connector
        return await voice_connector.execute(tool, routed_args)

    if provider == "data":
        from connectors.data_analysis import data_analysis_connector
        return await data_analysis_connector.execute(tool, routed_args)

    if provider == "chat_history":
        from connectors.chat_history import chat_history_connector
        return await chat_history_connector.execute(tool, routed_args, agent)

    if provider == "repo":
        from connectors.repo_workspace import repo_workspace_connector
        return await repo_workspace_connector.execute(tool, routed_args)

    if provider in {"computer", "local_computer"}:
        from connectors.computer import computer_connector
        return await computer_connector.execute(tool, routed_args)

    if provider == "desktop":
        from connectors.desktop import desktop_connector
        return await desktop_connector.execute(tool, routed_args)

    if provider == "canva":
        from connectors.canva import canva_connector
        return await canva_connector.execute(tool, routed_args)

    if provider == "mcp":
        from connectors.mcp_client import mcp_connector
        return await mcp_connector.execute(tool, routed_args, agent)

    if provider == "platform":
        from connectors.platform import platform_connector
        return await platform_connector.execute(tool, routed_args, agent)

    # Generic HTTP connector — handles any OAuth2-connected app (Notion, Slack, GitHub, etc.)
    from connectors.generic_http import generic_http_connector
    return await generic_http_connector.execute(tool, routed_args, vault_ref)


class ToolBroker:
    async def execute(self, agent: AgentContext, tool: str, args: dict) -> ToolResult:
        approved_by_gate = bool(args.pop("__approved_by_gate", False))
        idempotency_key = args.pop("__idempotency_key", None)
        _check_untrusted_source_policy(tool, args)
        args_hash = hashlib.sha256(json.dumps(args, sort_keys=True).encode()).hexdigest()

        # 1. Permission check (seam — always runs)
        await permissions.check(agent.as_member(), f"use_tool:{tool}", agent.workspace_id or "default")

        # 2. Rate limiting (per org, Redis-backed — survives restarts)
        if not (settings.demo_mode and tool == "gmail.draft"):
            await _check_rate_limit(agent.org_id)

        # 2b. Per-org daily token budget guard (Category 9).
        if settings.per_org_daily_token_limit > 0:
            await _check_token_budget(agent.org_id)

        # 3. Loop detection
        await _check_loop(agent.org_id, tool, args_hash)

        # 4. Hard safety limits (no override possible)
        _check_safety_limits(tool, args)

        # 5. Approval gate — the hard floor is absolute, even under full_auto.
        if tool in _ALWAYS_APPROVAL_TOOLS and not approved_by_gate:
            raise ApprovalRequired(tool, "tool requires an approval record (hard floor — never bypassable)")
        policy = await tool_policy(agent.org_id, tool.split(".")[0])
        if policy.get("enabled") is False:
            raise ApprovalRequired(tool, "tool is disabled in settings")
        # Graduated Autonomy gate. Prices the call, then decides allow vs. approval
        # against earned trust. full_auto still collapses the settings gate (handled
        # inside evaluate); the hard floor above and safety limits below are absolute
        # and run independently. Trust can only loosen governance, never break it.
        autonomy_level = await workspace_autonomy(agent.org_id, agent.workspace_id)
        risk = risk_pricer.price(tool, args)
        if not approved_by_gate:
            gate = await autonomy.evaluate(
                agent.org_id, agent.workspace_id, risk, args, policy, autonomy_level
            )
            if not gate.allow:
                raise ApprovalRequired(tool, gate.reason)

        if _is_external_write_tool(tool):
            cached = await _load_idempotent_result(agent.org_id, tool, idempotency_key)
            if cached:
                await audit.log(
                    "tool_result",
                    agent.id,
                    tool,
                    organization_id=agent.org_id,
                    payload={"summary": cached.summary, "idempotency": "replayed"},
                )
                return cached

        # 6. Audit: tool_call before execution
        await audit.log(
            "tool_call",
            agent.id,
            tool,
            organization_id=agent.org_id,
            payload={
                "args_hash": args_hash,
                "idempotency_key": hashlib.sha256(str(idempotency_key).encode()).hexdigest()
                if idempotency_key else None,
            },   # never log raw args — they may contain credentials
        )

        # 7. Resolve the connector tier. Fixture/demo tiers keep tasks usable
        # when external OAuth or browser dependencies are not configured.
        provider = tool.split(".")[0]
        tier = await connector_tier(provider)
        if tier == "live" and provider not in {"browser", "fs", "code", "doc", "image", "voice", "data", "chat_history", "repo", "computer", "local_computer", "desktop", "canva", "skill", "platform"}:
            from connectors.registry import get as registry_get

            connector = await registry_get(agent, tool)
            # vault_ref is the only credential identifier that touches logs
            vault_ref = connector.vault_ref
        else:
            vault_ref = tier

        # 8. Execute via connector
        result = await _route(agent, tool, args, vault_ref, tier)
        result = _mark_untrusted_connector_result(tool, result)
        # Tell the model when it's looking at placeholder data (e.g. gmail demo
        # storage, browser fixtures) so it doesn't act on stub results as if real.
        _degraded = await degraded_note(provider)
        if _degraded:
            result = _annotate_degraded_result(result, _degraded)

        # 9. Audit: result summary (never log result.data — may contain sensitive content)
        await audit.log(
            "tool_result",
            agent.id,
            tool,
            organization_id=agent.org_id,
            payload={"summary": result.summary},
        )
        if _is_external_write_tool(tool):
            await _store_idempotent_result(agent.org_id, tool, idempotency_key, result)

        # Feed the trust ledger: an action that ran unattended is positive evidence
        # ("auto_success"); one that a human approved is weaker positive evidence
        # ("approved"). Best-effort — a missing ledger never affects the result.
        await trust.record_outcome(
            agent.org_id,
            agent.workspace_id,
            risk,
            "approved" if approved_by_gate else "auto_success",
            region=settings.region,
            tool=tool,
            actor_id=agent.id,
        )

        return result


tool_broker = ToolBroker()


async def execute(agent: AgentContext, tool: str, args: dict) -> ToolResult:
    """Public seam function — signature is frozen at (agent, tool, args) → ToolResult."""
    return await tool_broker.execute(agent, tool, args)
