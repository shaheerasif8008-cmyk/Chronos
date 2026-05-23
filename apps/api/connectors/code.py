from __future__ import annotations

import asyncio
import os
import re
import resource
import sys
from pathlib import Path
from typing import Any

from core.models import ToolResult

WORKSPACE_ROOT = Path("/tmp/chronos_task_workspaces")
MAX_CODE_BYTES = 64_000
MAX_OUTPUT_BYTES = 64_000
DEFAULT_TIMEOUT_SECONDS = 5
FORBIDDEN_PATTERNS = [
    r"\bimport\s+(socket|requests|httpx|urllib|subprocess|multiprocessing|ctypes|resource)\b",
    r"\bfrom\s+(socket|requests|httpx|urllib|subprocess|multiprocessing|ctypes|resource)\b",
    r"__import__\s*\(",
    r"\bopen\s*\(\s*['\"]/",
    r"\bos\.(system|popen|spawn|exec)",
]


def _workspace_root(args: dict[str, Any]) -> Path:
    org_id = str(args.pop("__org_id", "default") or "default")
    task_id = str(args.pop("__task_id", "manual") or "manual")
    root = (WORKSPACE_ROOT / org_id / task_id).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


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
    async def execute(self, tool: str, args: dict[str, Any]) -> ToolResult:
        args.pop("__connector_tier", None)
        if tool != "code.python":
            raise ValueError(f"Unknown code tool: {tool}")
        return await self._python(args)

    async def _python(self, args: dict[str, Any]) -> ToolResult:
        root = _workspace_root(args)
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
