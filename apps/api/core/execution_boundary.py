"""Production boundary for user-controlled execution.

Development and test environments may use the lightweight in-process
subprocess implementations for local ergonomics.  They are not security
boundaries, however: a cwd jail, a lexical denylist, and RLIMITs do not isolate
the child from the API container's PID namespace, filesystem, credentials, or
task role.

Production therefore permits arbitrary command execution only through a
separate sandbox runtime. ``code.*``, ``data.*``, ``computer.*``, and ``repo.*``
have E2B-backed production paths; ``local_computer.*`` uses an authenticated
paired device and never executes in the API container. Remaining host-only
providers return a truthful unavailable result before they touch the filesystem
or spawn a process.
"""
from __future__ import annotations

from core.models import ToolResult


_HOST_BACKED_PROVIDER_PREFIXES = (
    "desktop.",
)


def api_host_execution_allowed() -> bool:
    """Whether API-container subprocess execution is allowed.

    This intentionally has no production override.  A misspelled or staging
    environment is already treated as production by ``Settings.is_production``;
    operators must use a genuine isolated runtime instead of weakening the
    boundary with another environment flag.
    """

    # Resolve settings at the decision point. Besides making configuration
    # reloads deterministic, this prevents a module imported during bootstrap
    # from retaining a stale Settings object after the application replaces
    # its configuration seam.
    from core.config import settings

    return not bool(getattr(settings, "is_production", False))


def blocks_api_host_tool(tool: str) -> bool:
    return (not api_host_execution_allowed()) and tool.startswith(_HOST_BACKED_PROVIDER_PREFIXES)


def unavailable_host_execution_result(tool: str) -> ToolResult:
    """Return the stable truthful-degraded envelope for a blocked host tool."""

    reason = (
        f"{tool} is disabled in production because API-container process execution "
        "is not an isolation boundary. Use the isolated computer runtime instead."
    )

    return ToolResult(
        data={
            "status": "unavailable",
            "reason": reason,
            "execution_boundary": "isolated_runtime_required",
            "host_execution": False,
        },
        summary=f"{tool} unavailable: isolated runtime required",
    )
