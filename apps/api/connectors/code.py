from __future__ import annotations

import asyncio
import os
import re
import resource
import sys
from typing import Any

from connectors.e2b_runtime import (
    RuntimeUnavailable,
    SANDBOX_ROOT,
    SandboxRuntime,
    default_runtime,
)
from core.config import settings
from core.execution_boundary import api_host_execution_allowed, unavailable_host_execution_result
from core.models import ToolResult
from core.workspace import task_workspace_root_from_args

MAX_CODE_BYTES = 64_000
MAX_OUTPUT_BYTES = 64_000
DEFAULT_TIMEOUT_SECONDS = 5
# NOTE: this is a defence-in-depth denylist, NOT a security boundary. A lexical
# blocklist cannot fully contain Python (e.g. ``().__class__.__bases__[0]
# .__subclasses__()`` reaches arbitrary classes without any blocked token). The
# production isolation is the E2B sandbox; RLIMITs and the per-task cwd only
# reduce risk in the explicitly development-only host implementation. Patterns
# below close cheap, common bypasses in both paths, but are never treated as the
# security boundary.
FORBIDDEN_PATTERNS = [
    r"\bimport\s+(socket|requests|httpx|urllib|subprocess|multiprocessing|ctypes|resource)\b",
    r"\bfrom\s+(socket|requests|httpx|urllib|subprocess|multiprocessing|ctypes|resource)\b",
    r"__import__\s*\(",
    r"\bimportlib\b",
    r"\bimport_module\s*\(",
    r"\bimport\s+builtins\b",
    r"\bfrom\s+builtins\b",
    r"\bopen\s*\(\s*['\"]/",
    r"\bio\.open\s*\(\s*['\"]/",
    r"\bPath\s*\(\s*['\"]/",
    r"\bpathlib\.Path\s*\(\s*['\"]/",
    r"\bos\.(listdir|scandir|walk|stat|lstat|readlink)\s*\(\s*['\"]/",
    r"\bos\.(system|popen|spawn|exec)",
    r"\beval\s*\(",
    r"\bexec\s*\(",
    r"\bcompile\s*\(",
    r"\bbreakpoint\s*\(",
    r"__builtins__",
    r"__subclasses__",
    r"__bases__",
    r"__mro__",
    r"__globals__",
    r"\.\./",            # relative path traversal out of the task workspace
]


def _set_limits() -> None:
    for limit, value in (
        (resource.RLIMIT_CPU, (2, 2)),
        (resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024)),
        (resource.RLIMIT_FSIZE, (2 * 1024 * 1024, 2 * 1024 * 1024)),
    ):
        try:
            resource.setrlimit(limit, value)
        except (OSError, ValueError):
            continue


def _validate_code(code: str) -> None:
    if len(code.encode("utf-8")) > MAX_CODE_BYTES:
        raise ValueError(f"code.python payload exceeds {MAX_CODE_BYTES} bytes")
    for pattern in FORBIDDEN_PATTERNS:
        if re.search(pattern, code):
            raise ValueError("code.python rejected an unsafe import or filesystem operation")


class CodeConnector:
    def __init__(self, runtime: SandboxRuntime | None = None) -> None:
        self._runtime = runtime

    async def execute(self, tool: str, args: dict[str, Any]) -> ToolResult:
        args.pop("__connector_tier", None)
        if tool != "code.python":
            raise ValueError(f"Unknown code tool: {tool}")
        if settings.is_production:
            return await self._python_isolated(args)
        if not api_host_execution_allowed():
            return unavailable_host_execution_result(tool)
        return await self._python(args)

    def _isolated_runtime(self) -> SandboxRuntime | None:
        return self._runtime if self._runtime is not None else default_runtime()

    async def _python_isolated(self, args: dict[str, Any]) -> ToolResult:
        """Execute Python in an ephemeral E2B sandbox in production."""

        org_id = str(args.pop("__org_id", "default") or "default")
        task_id = str(args.pop("__task_id", "manual") or "manual")
        code = str(args.get("code") or "")
        timeout_seconds = min(int(args.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS), 10)
        _validate_code(code)

        runtime = self._isolated_runtime()
        if runtime is None:
            return ToolResult(
                data={
                    "status": "unavailable",
                    "reason": "code.python requires the isolated E2B runtime; set E2B_API_KEY.",
                    "execution_boundary": "isolated_runtime_required",
                    "host_execution": False,
                },
                summary="code.python unavailable: isolated runtime required",
            )

        sandbox_id: str | None = None
        try:
            sandbox_id = await runtime.create(
                timeout_seconds=min(settings.e2b_sandbox_timeout_seconds, 120),
                metadata={"org": org_id, "task": task_id, "tool": "code.python"},
            )
            remote_script = f"{SANDBOX_ROOT}/code.py"
            await runtime.write(sandbox_id, remote_script, code.encode("utf-8"))
            result = await runtime.run(
                sandbox_id,
                "python3 code.py",
                cwd=SANDBOX_ROOT,
                timeout_seconds=timeout_seconds,
            )
        except RuntimeUnavailable as exc:
            return ToolResult(
                data={
                    "status": "unavailable",
                    "reason": f"isolated code runtime unavailable: {exc}",
                    "execution_boundary": "isolated_runtime_required",
                    "host_execution": False,
                },
                summary="code.python unavailable: isolated runtime failed",
            )
        except Exception as exc:  # noqa: BLE001 - provider errors vary
            return ToolResult(
                data={
                    "status": "failure",
                    "reason": f"isolated code execution failed: {type(exc).__name__}",
                    "execution_boundary": "isolated_runtime",
                    "host_execution": False,
                },
                summary="code.python failed in isolated runtime",
            )
        finally:
            if sandbox_id is not None:
                try:
                    await runtime.kill(sandbox_id)
                except Exception:
                    # E2B's TTL remains the cleanup backstop.
                    pass

        stdout = str(result.get("stdout") or "")
        stderr = str(result.get("stderr") or "")
        stdout_b = stdout.encode("utf-8")
        stderr_b = stderr.encode("utf-8")
        status = str(result.get("status") or "failure")
        return ToolResult(
            data={
                "status": status,
                "returncode": result.get("returncode"),
                "stdout": stdout_b[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace"),
                "stderr": stderr_b[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace"),
                "stdout_truncated": bool(result.get("stdout_truncated"))
                or len(stdout_b) > MAX_OUTPUT_BYTES,
                "stderr_truncated": bool(result.get("stderr_truncated"))
                or len(stderr_b) > MAX_OUTPUT_BYTES,
                "workspace": SANDBOX_ROOT,
                "execution_boundary": "isolated_runtime",
                "host_execution": False,
            },
            summary=f"Python execution {status} in isolated runtime",
        )

    async def _python(self, args: dict[str, Any]) -> ToolResult:
        root = task_workspace_root_from_args(args)
        code = str(args.get("code") or "")
        timeout_seconds = min(int(args.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS), 10)
        _validate_code(code)

        env = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "HOME": str(root),
        }
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            "-S",
            "-c",
            code,
            cwd=str(root),
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            preexec_fn=_set_limits if os.name == "posix" else None,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
            timed_out = False
        except asyncio.TimeoutError:
            process.kill()
            stdout, stderr = await process.communicate()
            timed_out = True

        out = stdout[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
        err = stderr[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
        status = "timeout" if timed_out else ("success" if process.returncode == 0 else "failure")
        return ToolResult(
            data={
                "status": status,
                "returncode": process.returncode,
                "stdout": out,
                "stderr": err,
                "stdout_truncated": len(stdout) > MAX_OUTPUT_BYTES,
                "stderr_truncated": len(stderr) > MAX_OUTPUT_BYTES,
                "workspace": str(root),
            },
            summary=f"Python execution {status}",
        )


code_connector = CodeConnector()
