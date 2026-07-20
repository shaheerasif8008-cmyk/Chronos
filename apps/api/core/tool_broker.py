from __future__ import annotations

"""
Tool Broker — the single gateway for ALL connector calls.

Every tool call routes through execute(). No direct connector calls. Ever.
Signature is frozen: execute(agent, tool, args) → ToolResult.
"""
import hashlib
import importlib
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

audit = importlib.import_module("core.audit")
autonomy = importlib.import_module("core.autonomy")
permissions = importlib.import_module("core.permissions")
risk_pricer = importlib.import_module("core.risk")
risk_registry = importlib.import_module("core.risk_registry")
trust = importlib.import_module("core.trust")
from core.connector_health import connector_tier, degraded_note
from core.connector_write_ledger import (
    ConnectorWriteLedger,
    ManualReviewRequired,
    WriteOperationBusy,
    WriteOperationConflict,
    WriteOperationTerminal,
    is_broker_connector_mutation,
    provider_supports_idempotency,
)
from core.config import settings
from core.execution_boundary import (
    blocks_api_host_tool,
    unavailable_host_execution_result,
)
from core.exceptions import (
    ApprovalRequired,
    ConnectorNotFound,
    LoopDetected,
    RateLimitExceeded,
    SafetyLimitViolation,
)
from core.governance import enforce_request_rate, suspend_org
from core.models import AgentContext, ToolResult
from core.redis import redis_client
from core.settings_store import tool_policy, workspace_autonomy
from core.token_budget import (
    record_tokens_used as _record_tokens_used,
    tokens_used_today,
)
from core.untrusted_content import scan_untrusted_content

# The hard floor: tools that always require a human approval record — regardless
# of autonomy level, including ``full_auto``. External publish, payments, mass
# email (gmail.send — see RULE 8), and local-machine shell/app launches can never
# be bypassed by a workspace running in full-auto.
_ALWAYS_APPROVAL_TOOLS = {
    "twitter.post",
    "linkedin.post",
    "website.publish",
    "gmail.send",
    "slack.send",
    "github.create_issue",
    "google_drive.upload",
    "local_computer.exec",
    "local_computer.open_app",
    "desktop.open_app",  # launching an app into the virtual desktop is risk-tiered
    "repo.create_pr",
    # E2B cloud-computer creation costs provider resources; shell/package work
    # and raw GUI input can change external state; cancellation destroys all
    # unexported sandbox state. They therefore retain a hard approval floor.
    "computer.create_session",
    "computer.exec",
    "computer.install_package",
    "computer.input",
    "computer.cancel_session",
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
    return tool in _ALWAYS_APPROVAL_TOOLS or any(
        marker in tool for marker in _WRITE_ACTION_MARKERS
    )


def _cache_key(
    org_id: str,
    tool: str,
    idempotency_key: str,
    *,
    member_id: str | None = None,
) -> str:
    # Preserve the existing cache namespace for other external-write tools.
    # Gmail send additionally scopes by the credential-owning member so one
    # tenant member can never replay another member's delivery result.
    scope = f"{org_id}:{member_id}" if member_id else org_id
    digest = hashlib.sha256(f"{scope}:{tool}:{idempotency_key}".encode()).hexdigest()
    return f"idempotency:{digest}"


async def _load_idempotent_result(
    org_id: str,
    tool: str,
    idempotency_key: str | None,
    *,
    member_id: str | None = None,
) -> ToolResult | None:
    if not idempotency_key:
        return None
    raw = await redis_client.get(
        _cache_key(org_id, tool, idempotency_key, member_id=member_id)
    )
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
    return ToolResult(
        summary=str(result.get("summary") or ""), data=result.get("data") or {}
    )


async def _store_idempotent_result(
    org_id: str,
    tool: str,
    idempotency_key: str | None,
    result: ToolResult,
    *,
    member_id: str | None = None,
) -> None:
    if not idempotency_key:
        return
    payload = {
        "tool": tool,
        "result": {"summary": result.summary, "data": result.data},
        "stored_at": int(time.time()),
    }
    await redis_client.set(
        _cache_key(org_id, tool, idempotency_key, member_id=member_id),
        json.dumps(payload, default=str),
        ex=_IDEMPOTENCY_TTL_SECONDS,
    )


def _check_untrusted_source_policy(tool: str, args: dict[str, Any]) -> None:
    if not args.pop("__triggered_by_untrusted_content", False):
        return
    if _is_external_write_tool(tool):
        raise ApprovalRequired(
            tool,
            "untrusted external content cannot trigger write actions without approval",
        )


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
    if (
        provider not in _UNTRUSTED_PROVIDER_PREFIXES
        or _is_external_write_tool(tool)
        or result.data.get("untrusted_content")
    ):
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
    await enforce_request_rate(org_id, scope="connector")


async def _check_loop(org_id: str, tool: str, args_hash: str) -> None:
    key = f"loop:{org_id}:{tool}:{args_hash}"
    count = await redis_client.incr(key)
    if count == 1:
        await redis_client.expire(key, 300)  # 5-minute window
    if count >= _LOOP_THRESHOLD:
        await suspend_org(org_id, f"loop detected for {tool}", actor_id="tool_broker")
        raise LoopDetected(tool, count)


def _check_safety_limits(tool: str, args: dict) -> None:
    if tool == "gmail.send":
        # The connector reuses the same validator immediately before provider
        # dispatch. Running it here keeps invalid/oversized payloads out of the
        # approval inbox as well as enforcing the hard floor at execution time.
        from connectors.gmail_delivery import validate_email_args

        validate_email_args(args)

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
        risky_markers = (
            "rm -rf",
            "mkfs",
            "diskutil erase",
            "shutdown",
            "reboot",
            ":(){",
        )
        if any(marker in command for marker in risky_markers):
            raise SafetyLimitViolation(f"{tool}: command rejected by safety policy")

    # Generic delete guard
    if "delete" in tool:
        ids = args.get("ids", args.get("record_ids", []))
        count = args.get("count", len(ids) if isinstance(ids, list) else 0)
        if count > _MAX_DELETE_RECORDS:
            raise SafetyLimitViolation(
                f"{tool}: deleting {count} records exceeds limit of {_MAX_DELETE_RECORDS}"
            )

    # Financial guard
    if any(t in tool for t in ("finance.", "payment.")):
        amount = float(args.get("amount", 0))
        if amount > _MAX_FINANCIAL_AMOUNT:
            raise SafetyLimitViolation(
                f"{tool}: amount ${amount} exceeds limit of ${_MAX_FINANCIAL_AMOUNT}"
            )


async def _check_token_budget(org_id: str) -> None:
    """Enforce the per-org daily token budget by checking a Redis counter.

    The counter is incremented by `record_tokens_used` after each model call.
    This function just reads the current total and raises if over budget.
    """
    used = await tokens_used_today(org_id)
    if used >= settings.per_org_daily_token_limit:
        raise RateLimitExceeded(org_id, used, settings.per_org_daily_token_limit)


async def record_tokens_used(org_id: str, tokens: int) -> None:
    """Increment the per-org daily token counter. Call after each model completion."""
    await _record_tokens_used(org_id, tokens)


async def _route_skill_run_script(
    agent: AgentContext, args: dict, tier: str
) -> ToolResult:
    """Run a bundled skill script only inside the configured isolated runtime."""
    import json
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
        raise FileNotFoundError(
            f"skill.run_script: script not found: {skill_id}/{script_name}"
        )
    if script_path.suffix not in {".py", ".sh"}:
        raise ValueError("skill.run_script supports only .py and .sh scripts")

    # Per-script permission check (RULE 3 — never inline permission logic).
    await permissions.check(
        agent.as_member(), "skill.run_script", f"skill:{skill_id}/{script_name}"
    )

    from connectors.e2b_runtime import RuntimeUnavailable, SANDBOX_ROOT, default_runtime

    runtime = default_runtime()
    if runtime is None:
        return ToolResult(
            data={
                "status": "unavailable",
                "reason": "skill.run_script requires the isolated E2B runtime; set E2B_API_KEY.",
                "execution_boundary": "isolated_runtime_required",
                "host_execution": False,
            },
            summary="skill.run_script unavailable: isolated runtime required",
        )

    sandbox_id: str | None = None
    try:
        sandbox_id = await runtime.create(
            timeout_seconds=min(
                int(getattr(settings, "e2b_sandbox_timeout_seconds", 1800)), 120
            ),
            metadata={
                "org": agent.org_id,
                "task": agent.task_id or agent.id,
                "skill": skill_id,
            },
        )
        remote_script = f"{SANDBOX_ROOT}/skill{script_path.suffix}"
        remote_params = f"{SANDBOX_ROOT}/params.json"
        await runtime.write(sandbox_id, remote_script, script_path.read_bytes())
        await runtime.write(
            sandbox_id,
            remote_params,
            json.dumps(params, default=str).encode("utf-8"),
        )
        interpreter = "python3" if script_path.suffix == ".py" else "sh"
        result = await runtime.run(
            sandbox_id,
            f"{interpreter} {remote_script} < {remote_params}",
            cwd=SANDBOX_ROOT,
            timeout_seconds=60,
        )
    except RuntimeUnavailable as exc:
        return ToolResult(
            data={
                "status": "unavailable",
                "reason": f"isolated skill runtime unavailable: {exc}",
                "execution_boundary": "isolated_runtime_required",
                "host_execution": False,
            },
            summary="skill.run_script unavailable: isolated runtime failed",
        )
    except Exception as exc:
        return ToolResult(
            data={
                "status": "failure",
                "reason": f"isolated skill execution failed: {type(exc).__name__}",
                "execution_boundary": "isolated_runtime",
                "host_execution": False,
            },
            summary="skill.run_script failed in isolated runtime",
        )
    finally:
        if sandbox_id is not None:
            try:
                await runtime.kill(sandbox_id)
            except Exception:
                # Sandbox TTL is the final cleanup guarantee. A provider-side
                # teardown failure must not turn a completed script into a 500.
                pass

    stdout = str(result.get("stdout") or "")
    stderr = str(result.get("stderr") or "")
    parsed: object | None = None
    if result.get("status") == "success" and stdout.strip():
        try:
            parsed = json.loads(stdout)
        except json.JSONDecodeError:
            parsed = None
    return ToolResult(
        data={
            "status": result.get("status") or "failure",
            "returncode": result.get("returncode"),
            "stdout": stdout,
            "stderr": stderr,
            "result": parsed,
            "execution_boundary": "isolated_runtime",
            "host_execution": False,
        },
        summary=(
            f"skill.run_script {result.get('status') or 'failure'} in isolated runtime"
        ),
    )


async def _route(
    agent: AgentContext, tool: str, args: dict, vault_ref: str, tier: str = "live"
) -> ToolResult:
    """Route to the correct connector after all checks pass."""
    provider = tool.split(".")[0]
    if blocks_api_host_tool(tool):
        return unavailable_host_execution_result(tool)
    routed_args = dict(args)
    routed_args["__connector_tier"] = tier
    routed_args["__org_id"] = agent.org_id
    routed_args["__task_id"] = agent.task_id or agent.id
    # Real request/runtime contexts always carry the authenticated member ID.
    # Some legacy callers and isolated unit-test contexts predate that field;
    # omitting the internal hint preserves their connector defaults without
    # weakening propagation for authenticated production requests.
    member_id = getattr(agent, "member_id", None)
    if member_id:
        routed_args["__member_id"] = str(member_id)

    if tool == "skill.run_script":
        return await _route_skill_run_script(agent, args, tier)

    # Composio-managed SaaS providers (gmail, slack, notion, github, …) when a
    # COMPOSIO_API_KEY is configured. Managed auth lives in Composio, so this
    # branch needs no vault_ref — it is flagged with the "composio" sentinel.
    if vault_ref == "composio":
        from connectors.composio_connector import composio_connector

        return await composio_connector.execute(tool, routed_args, agent)

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
        if tool in {
            "doc.create",
            "doc.create_slides",
            "doc.fill_pdf",
            "doc.render_chart",
            "doc.detect_fields",
            "doc.verify_fill",
        }:
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


async def _dispatch_claimed_connector_write(
    dispatch: Callable[[], Awaitable[ToolResult]],
    *,
    ledger: ConnectorWriteLedger,
    operation: dict[str, Any],
    organization_id: str,
    tool: str,
) -> tuple[ToolResult, bool]:
    """Dispatch one claimed write and durably classify its outcome immediately."""

    operation_id = str(operation["id"])
    try:
        result = await dispatch()
    except Exception as exc:
        updated = await ledger.mark_ambiguous(
            operation_id,
            organization_id=organization_id,
            error=exc,
        )
        if updated and updated.get("status") == "retry":
            return (
                ToolResult(
                    data={
                        "status": "retry",
                        "manual_review_required": False,
                        "provider_idempotency_key_reused": True,
                    },
                    summary=f"{tool} raised after dispatch; a safe idempotent retry is pending",
                ),
                False,
            )
        return (
            ToolResult(
                data={"status": "manual_review", "manual_review_required": True},
                summary=f"{tool} raised after dispatch; automatic retry is disabled",
            ),
            False,
        )

    # This is deliberately the first await after provider return. A process
    # crash during audit/annotation can then adopt a known response without
    # issuing a second provider mutation.
    if result.data.get("status") == "ambiguous":
        updated = await ledger.mark_ambiguous(
            operation_id,
            organization_id=organization_id,
            error=str(result.data.get("error") or result.summary),
        )
        if updated and updated.get("status") == "retry":
            return (
                ToolResult(
                    data={
                        **result.data,
                        "status": "retry",
                        "manual_review_required": False,
                        "provider_idempotency_key_reused": True,
                    },
                    summary=f"{tool} outcome is ambiguous; a safe idempotent retry is pending",
                ),
                False,
            )
        return (
            ToolResult(
                data={
                    **result.data,
                    "status": "manual_review",
                    "manual_review_required": True,
                },
                summary=f"{tool} outcome is ambiguous; automatic retry is disabled",
            ),
            False,
        )
    if result.data.get("error"):
        await ledger.mark_failed(
            operation_id,
            organization_id=organization_id,
            error=str(result.data.get("error")),
        )
        return result, False
    await ledger.record_provider_response(
        operation_id,
        organization_id=organization_id,
        result={"summary": result.summary, "data": result.data},
        evidence={"tool": tool, "status": "success"},
    )
    return result, True


class ToolBroker:
    async def execute(self, agent: AgentContext, tool: str, args: dict) -> ToolResult:
        approved_by_gate = bool(args.pop("__approved_by_gate", False))
        approval_id = str(args.pop("__approval_id", "") or "")
        idempotency_key = args.pop("__idempotency_key", None)
        _check_untrusted_source_policy(tool, args)
        args_hash = hashlib.sha256(
            json.dumps(args, sort_keys=True).encode()
        ).hexdigest()
        from connectors.composio_client import (
            is_composio_provider as _is_composio_provider,
            is_configured as _composio_configured,
        )
        from connectors.oauth_apps import get_app as _get_oauth_app

        generic_provider = bool(_get_oauth_app(tool.split(".")[0])) or (
            _composio_configured() and _is_composio_provider(tool.split(".")[0])
        )

        potential_durable_write = is_broker_connector_mutation(
            tool,
            args,
            composio=generic_provider,
        )

        # Project tool defaults are an execution allowlist, not merely a prompt
        # hint. Re-check at the broker so a resumed/stale model call cannot run a
        # tool that an owner removed after the task was planned.
        if agent.project_id:
            from core.project_access import project_tool_allowlist, tool_is_allowed

            try:
                project_tools = await project_tool_allowlist(agent.org_id, agent.project_id)
            except Exception as exc:
                raise SafetyLimitViolation(
                    "Project tool policy could not be verified"
                ) from exc
            if not tool_is_allowed(project_tools, tool):
                raise SafetyLimitViolation(
                    f"{tool} is not allowed by this project's default tool policy"
                )

        # 1. Permission check (seam — always runs)
        await permissions.check(
            agent.as_member(), f"use_tool:{tool}", agent.workspace_id or "default"
        )

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
            raise ApprovalRequired(
                tool, "tool requires an approval record (hard floor — never bypassable)"
            )
        if (
            tool in {"gmail.send", "repo.create_pr"}
            and approved_by_gate
            and (not approval_id or not idempotency_key)
        ):
            raise SafetyLimitViolation(
                f"{tool}: approved execution requires approval and idempotency evidence"
            )
        # Per-tool permissions (Settings → Connectors): blocked tools never run,
        # require_approval forces an approval record, always_allow skips only the
        # settings/autonomy gate below (never the hard floor or safety limits).
        from core.settings_store import tool_permissions as org_tool_permissions

        tool_permission = (await org_tool_permissions(agent.org_id)).get(
            tool, "default"
        )
        if tool_permission == "blocked":
            raise SafetyLimitViolation(
                f"{tool} is blocked by connector tool permissions (Settings → Connectors)"
            )
        if tool_permission == "require_approval" and not approved_by_gate:
            raise ApprovalRequired(
                tool, "tool requires approval by connector tool permissions"
            )
        policy = await tool_policy(agent.org_id, tool.split(".")[0])
        if policy.get("enabled") is False:
            raise ApprovalRequired(tool, "tool is disabled in settings")
        # Graduated Autonomy gate. Prices the call, then decides allow vs. approval
        # against earned trust. full_auto still collapses the settings gate (handled
        # inside evaluate); the hard floor above and safety limits below are absolute
        # and run independently. Trust can only loosen governance, never break it.
        autonomy_level = await workspace_autonomy(agent.org_id, agent.workspace_id)
        # Price the call against admin risk overrides and the action_class's track
        # record (established actions price slightly lower via novelty).
        overrides = await risk_registry.get_overrides(agent.org_id)
        provisional_class = risk_pricer.action_class(tool, args)
        level = await trust.get_trust_level(
            agent.org_id, agent.workspace_id, provisional_class
        )
        novelty = trust.novelty_from_successes(level.successes)
        risk = risk_pricer.price(tool, args, novelty=novelty, overrides=overrides)
        if not approved_by_gate and tool_permission != "always_allow":
            gate = await autonomy.evaluate(
                agent.org_id, agent.workspace_id, risk, args, policy, autonomy_level
            )
            if not gate.allow:
                raise ApprovalRequired(tool, gate.reason)

        if _is_external_write_tool(tool) and not potential_durable_write:
            cached = await _load_idempotent_result(
                agent.org_id,
                tool,
                idempotency_key,
                member_id=agent.member_id if tool == "gmail.send" else None,
            )
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
                "idempotency_key": hashlib.sha256(
                    str(idempotency_key).encode()
                ).hexdigest()
                if idempotency_key
                else None,
            },  # never log raw args — they may contain credentials
        )

        # 7. Resolve the connector tier. Fixture/demo tiers keep tasks usable
        # when external OAuth or browser dependencies are not configured.
        provider = tool.split(".")[0]
        tier = await connector_tier(provider)
        from connectors.composio_client import (
            entity_id as composio_entity_id,
            is_composio_provider,
            is_configured as composio_configured,
            parse_managed_vault_ref,
        )

        managed_composio = False
        connector = None
        if composio_configured() and is_composio_provider(provider):
            # Composio managed auth — credentials live in Composio, not the vault.
            # A deployment may still contain a valid legacy vault-backed OAuth
            # connection while it migrates to Composio. Dispatch through Composio
            # only when the selected member-scoped connector carries the managed
            # sentinel; never reinterpret an unrelated vault ref.
            from connectors.registry import get as registry_get

            try:
                connector = await registry_get(agent, tool)
            except ConnectorNotFound:
                if tier not in {"demo", "fixture"}:
                    raise
            if connector is not None:
                parsed = parse_managed_vault_ref(connector.vault_ref)
                expected_entity = composio_entity_id(agent.org_id, agent.member_id)
                if parsed and parsed != (provider, expected_entity):
                    raise ConnectorNotFound(agent.org_id, provider)
                managed_composio = parsed == (provider, expected_entity)
        if managed_composio:
            # The "composio" sentinel tells _route() to dispatch to the Composio
            # connector. A managed active connection is a live path.
            vault_ref = "composio"
            tier = "live"
        elif tier == "live" and provider not in {
            "browser",
            "fs",
            "code",
            "doc",
            "image",
            "voice",
            "data",
            "chat_history",
            "repo",
            "computer",
            "local_computer",
            "desktop",
            "canva",
            "skill",
            "platform",
        }:
            from connectors.registry import get as registry_get

            connector = connector or await registry_get(agent, tool)
            # vault_ref is the only credential identifier that touches logs
            vault_ref = connector.vault_ref
        else:
            vault_ref = tier

        durable_write = tier == "live" and is_broker_connector_mutation(
            tool,
            args,
            composio=managed_composio
            or bool(_get_oauth_app(provider))
            or (_composio_configured() and _is_composio_provider(provider)),
        )
        # A tool can be classified as a potential durable mutation before its
        # connector tier is known, then resolve to a fixture/demo connector at
        # runtime. Those non-live paths do not use the Postgres write ledger,
        # so they must still consult the legacy Redis result cache before
        # dispatch. Without this late check, retries of fixture/demo writes
        # execute the connector twice even with the same idempotency key.
        if _is_external_write_tool(tool) and not durable_write and potential_durable_write:
            cached = await _load_idempotent_result(
                agent.org_id,
                tool,
                idempotency_key,
                member_id=agent.member_id if tool == "gmail.send" else None,
            )
            if cached:
                await audit.log(
                    "tool_result",
                    agent.id,
                    tool,
                    organization_id=agent.org_id,
                    payload={"summary": cached.summary, "idempotency": "replayed"},
                )
                return cached
        write_ledger: ConnectorWriteLedger | None = None
        write_operation: dict[str, Any] | None = None
        if durable_write:
            from connectors.framework.repository import DatabaseConnectorRepository

            write_ledger = ConnectorWriteLedger(DatabaseConnectorRepository())
            bound_idempotency_key = str(
                idempotency_key
                or f"broker:{agent.task_id or agent.id}:{tool}:{args_hash}"
            )
            try:
                write_operation = await write_ledger.prepare(
                    organization_id=agent.org_id,
                    member_id=agent.member_id,
                    task_id=str(agent.task_id or agent.id),
                    channel="broker",
                    tool=tool,
                    provider=provider,
                    risk_level=str(getattr(risk, "level", None) or "write"),
                    payload=args,
                    approval_binding=approval_id
                    if approved_by_gate
                    else "autonomy-policy",
                    idempotency_key=bound_idempotency_key,
                    provider_idempotency=(
                        provider_supports_idempotency(provider) and not managed_composio
                    ),
                )
                claim = await write_ledger.claim(
                    str(write_operation["id"]),
                    organization_id=agent.org_id,
                    owner=f"tool-broker:{uuid.uuid4()}",
                )
            except WriteOperationConflict as exc:
                raise SafetyLimitViolation(str(exc)) from exc
            except WriteOperationBusy:
                return ToolResult(
                    data={
                        "status": "in_progress",
                        "write_operation_id": str(write_operation["id"])
                        if write_operation
                        else None,
                    },
                    summary=f"{tool} is already in progress; no duplicate provider call was made",
                )
            except ManualReviewRequired as exc:
                return ToolResult(
                    data={
                        "status": "manual_review",
                        "manual_review_required": True,
                        "error": str(exc),
                    },
                    summary=f"{tool} requires manual provider-state review; automatic retry is disabled",
                )
            except WriteOperationTerminal as exc:
                return ToolResult(
                    data={"status": "failure", "error": str(exc)},
                    summary=f"{tool} was not retried because its durable write operation is terminal",
                )
            if claim.kind == "replay":
                durable = claim.result or {}
                replayed = ToolResult(
                    summary=str(durable.get("summary") or f"{tool} → replayed"),
                    data=durable.get("data") or {},
                )
                await audit.log(
                    "tool_result",
                    agent.id,
                    tool,
                    organization_id=agent.org_id,
                    payload={"summary": replayed.summary},
                )
                return replayed
            write_operation = claim.operation

        # 8. Execute via connector. Irreversible Gmail delivery and GitHub PR
        # publication receive verified internal execution context so durable
        # provider evidence binds to the exact approval/idempotency key. These
        # fields are never model-facing.
        routed_args = dict(args)
        if tool in {"gmail.send", "repo.create_pr", "platform.invoke"}:
            routed_args.update(
                {
                    "__approved_by_gate": approved_by_gate,
                    "__approval_id": approval_id,
                    "__idempotency_key": idempotency_key,
                }
            )
        if write_operation:
            routed_args.update(
                {
                    "__write_operation_id": str(write_operation["id"]),
                    "__provider_idempotency_key": write_operation[
                        "provider_idempotency_key"
                    ],
                }
            )
        provider_response_recorded = False
        if write_ledger and write_operation:
            (
                result,
                provider_response_recorded,
            ) = await _dispatch_claimed_connector_write(
                lambda: _route(agent, tool, routed_args, vault_ref, tier),
                ledger=write_ledger,
                operation=write_operation,
                organization_id=agent.org_id,
                tool=tool,
            )
        else:
            result = await _route(agent, tool, routed_args, vault_ref, tier)
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
        if write_ledger and write_operation:
            if provider_response_recorded:
                await write_ledger.complete(
                    str(write_operation["id"]), organization_id=agent.org_id
                )
        elif _is_external_write_tool(tool):
            await _store_idempotent_result(
                agent.org_id,
                tool,
                idempotency_key,
                result,
                member_id=agent.member_id if tool == "gmail.send" else None,
            )

        # Feed the trust ledger: an action that ran unattended is positive evidence
        # ("auto_success"); one that a human approved is weaker positive evidence
        # ("approved"). Best-effort — a missing ledger never affects the result.
        if result.data.get("status") not in {
            "ambiguous",
            "manual_review",
            "retry",
            "failure",
        } and not result.data.get("error"):
            await trust.record_outcome(
                agent.org_id,
                agent.workspace_id,
                risk,
                "approved" if approved_by_gate else "auto_success",
                region=getattr(settings, "region", "us"),
                tool=tool,
                actor_id=agent.id,
            )

        return result


tool_broker = ToolBroker()


async def execute(agent: AgentContext, tool: str, args: dict) -> ToolResult:
    """Public seam function — signature is frozen at (agent, tool, args) → ToolResult."""
    return await tool_broker.execute(agent, tool, args)
