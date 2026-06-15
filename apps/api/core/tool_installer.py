from __future__ import annotations

import asyncio
import shutil
import sys
from dataclasses import dataclass
from typing import Sequence

from core import audit
from core.config import settings


@dataclass(frozen=True)
class ToolInstallResult:
    tool: str
    status: str
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    reason: str = ""


# Each tool id maps to a sequence of commands run in order; the install succeeds
# only if every command exits 0. System packages need an `apt-get update` first
# because the base image clears the apt lists. This stays a curated allowlist on
# purpose — it is not a generic shell runner.
_APT = ("apt-get", "install", "-y", "--no-install-recommends")
_SUPPORTED_RUNTIME_TOOLS: dict[str, tuple[tuple[str, ...], ...]] = {
    "playwright.chromium": ((sys.executable, "-m", "playwright", "install", "chromium"),),
    "git": (("apt-get", "update"), (*_APT, "git")),
    "ripgrep": (("apt-get", "update"), (*_APT, "ripgrep")),
}

# Map a required executable name to the allowlisted tool id that installs it, so
# connectors can self-heal by binary name without knowing the package id.
_BINARY_TO_TOOL: dict[str, str] = {
    "git": "git",
    "rg": "ripgrep",
}

_INSTALL_LOCKS: dict[str, asyncio.Lock] = {}
_INSTALLED_THIS_PROCESS: set[str] = set()
_MAX_OUTPUT_CHARS = 12_000


def supported_runtime_tools() -> set[str]:
    return set(_SUPPORTED_RUNTIME_TOOLS)


async def ensure_runtime_tool(
    tool: str,
    *,
    organization_id: str,
    actor_id: str = "chronos",
    reason: str = "runtime dependency missing",
) -> ToolInstallResult:
    """Install a small allowlisted runtime dependency when a connector proves it is missing.

    This is intentionally not a generic shell runner. Callers pass a stable tool id,
    and only ids in `_SUPPORTED_RUNTIME_TOOLS` map to commands.
    """

    if tool not in _SUPPORTED_RUNTIME_TOOLS:
        raise ValueError(f"runtime tool install is not supported for {tool}")
    if not settings.runtime_auto_install_tools:
        return ToolInstallResult(tool=tool, status="disabled", reason="runtime auto-install is disabled")
    if tool in _INSTALLED_THIS_PROCESS:
        return ToolInstallResult(tool=tool, status="already_installed", reason="installed earlier in this process")

    lock = _INSTALL_LOCKS.setdefault(tool, asyncio.Lock())
    async with lock:
        if tool in _INSTALLED_THIS_PROCESS:
            return ToolInstallResult(tool=tool, status="already_installed", reason="installed earlier in this process")
        result = await _run_allowlisted_commands(_SUPPORTED_RUNTIME_TOOLS[tool])
        status = "installed" if result.returncode == 0 else "failed"
        install_result = ToolInstallResult(
            tool=tool,
            status=status,
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            reason=reason,
        )
        if status == "installed":
            _INSTALLED_THIS_PROCESS.add(tool)
        await _audit_install(install_result, organization_id=organization_id, actor_id=actor_id)
        return install_result


async def ensure_binary(
    binary: str,
    *,
    organization_id: str,
    actor_id: str = "chronos",
    reason: str = "",
) -> bool:
    """Ensure an allowlisted system binary is on PATH, installing it on demand.

    Returns True if the binary is available (already or after a successful
    install). Returns False for binaries outside the curated allowlist or when an
    install attempt does not make the binary reachable — callers then degrade
    exactly as they did before. The install is also baked into the production
    image; this runtime path is the fallback for environments that lack it."""
    if shutil.which(binary):
        return True
    tool = _BINARY_TO_TOOL.get(binary)
    if not tool:
        return False
    result = await ensure_runtime_tool(
        tool,
        organization_id=organization_id,
        actor_id=actor_id,
        reason=reason or f"{binary} binary missing",
    )
    if result.status not in {"installed", "already_installed"}:
        return False
    return shutil.which(binary) is not None


async def _run_allowlisted_commands(commands: Sequence[Sequence[str]]) -> ToolInstallResult:
    """Run each allowlisted command in order; stop at the first non-zero exit."""
    last = ToolInstallResult(tool="", status="success", returncode=0)
    for command in commands:
        last = await _run_allowlisted_command(command)
        if last.returncode != 0:
            break
    return last


async def _run_allowlisted_command(command: Sequence[str]) -> ToolInstallResult:
    process = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=max(1.0, float(settings.runtime_tool_install_timeout_seconds)),
        )
    except asyncio.TimeoutError:
        process.kill()
        stdout, stderr = await process.communicate()
        return ToolInstallResult(
            tool="",
            status="timeout",
            returncode=process.returncode,
            stdout=stdout.decode("utf-8", errors="replace")[-_MAX_OUTPUT_CHARS:],
            stderr=stderr.decode("utf-8", errors="replace")[-_MAX_OUTPUT_CHARS:],
        )
    return ToolInstallResult(
        tool="",
        status="success" if process.returncode == 0 else "failure",
        returncode=process.returncode,
        stdout=stdout.decode("utf-8", errors="replace")[-_MAX_OUTPUT_CHARS:],
        stderr=stderr.decode("utf-8", errors="replace")[-_MAX_OUTPUT_CHARS:],
    )


async def _audit_install(result: ToolInstallResult, *, organization_id: str, actor_id: str) -> None:
    try:
        await audit.log(
            "activity",
            actor_id,
            "runtime_tool_install",
            organization_id=organization_id,
            payload={
                "type": "runtime_tool_install",
                "tool": result.tool,
                "status": result.status,
                "returncode": result.returncode,
                "reason": result.reason,
                "stdout_tail": result.stdout[-1000:],
                "stderr_tail": result.stderr[-1000:],
            },
        )
    except Exception:
        return
