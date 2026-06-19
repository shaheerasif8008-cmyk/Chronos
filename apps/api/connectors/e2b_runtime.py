"""Isolated runtime backing the ``computer.*`` tools.

The computer connector talks to this async interface instead of running shell on
the API host. The real implementation lazily imports the E2B SDK so the package
is only required when an operator configures the runtime; tests inject an
in-memory fake implementing the same methods.
"""
from __future__ import annotations

from typing import Any, Protocol

from core.config import settings

SANDBOX_ROOT = "/home/user/workspace"


class RuntimeUnavailable(RuntimeError):
    """Raised when the isolated runtime cannot be started."""


class SandboxExpired(RuntimeError):
    """Raised when a sandbox id no longer resolves."""


class SandboxRuntime(Protocol):
    async def create(self, *, timeout_seconds: int, metadata: dict[str, Any]) -> str: ...
    async def run(self, sandbox_id: str, command: str, *, cwd: str, timeout_seconds: int) -> dict[str, Any]: ...
    async def write(self, sandbox_id: str, path: str, content: bytes) -> None: ...
    async def read(self, sandbox_id: str, path: str) -> bytes: ...
    async def list(self, sandbox_id: str, path: str) -> list[dict[str, Any]]: ...
    async def kill(self, sandbox_id: str) -> None: ...


def remote_path(requested: str) -> str:
    """Resolve a model-supplied path under SANDBOX_ROOT, refusing escapes."""
    rel = (requested or ".").strip().lstrip("/")
    parts: list[str] = []
    for segment in rel.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            if not parts:
                raise ValueError("Path escapes the sandbox workspace")
            parts.pop()
            continue
        parts.append(segment)
    return "/".join([SANDBOX_ROOT, *parts]) if parts else SANDBOX_ROOT


class E2BRuntime:
    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    async def _connect(self, sandbox_id: str):
        try:
            from e2b import AsyncSandbox  # type: ignore
        except ImportError as exc:  # pragma: no cover - optional runtime
            raise RuntimeUnavailable("e2b SDK is not installed") from exc
        try:
            return await AsyncSandbox.connect(sandbox_id, api_key=self._api_key)
        except Exception as exc:  # noqa: BLE001 - SDK expiry errors vary
            raise SandboxExpired(sandbox_id) from exc

    async def create(self, *, timeout_seconds: int, metadata: dict[str, Any]) -> str:
        try:
            from e2b import AsyncSandbox  # type: ignore
        except ImportError as exc:  # pragma: no cover - optional runtime
            raise RuntimeUnavailable("e2b SDK is not installed") from exc
        sandbox = await AsyncSandbox.create(
            api_key=self._api_key,
            timeout=timeout_seconds,
            metadata={key: str(value) for key, value in metadata.items() if value is not None},
        )
        sandbox_id = getattr(sandbox, "sandbox_id", None) or getattr(sandbox, "id", None)
        if not sandbox_id:
            raise RuntimeUnavailable("e2b did not return a sandbox id")
        await sandbox.commands.run(f"mkdir -p {SANDBOX_ROOT}")
        return str(sandbox_id)

    async def run(self, sandbox_id: str, command: str, *, cwd: str, timeout_seconds: int) -> dict[str, Any]:
        sandbox = await self._connect(sandbox_id)
        timed_out = False
        try:
            result = await sandbox.commands.run(command, cwd=cwd, timeout=timeout_seconds)
            stdout = getattr(result, "stdout", "") or ""
            stderr = getattr(result, "stderr", "") or ""
            exit_code = int(getattr(result, "exit_code", 0) or 0)
        except Exception as exc:  # noqa: BLE001 - non-zero exits may surface here
            stdout = str(getattr(exc, "stdout", "") or "")
            stderr = str(getattr(exc, "stderr", "") or str(exc))
            exit_code = int(getattr(exc, "exit_code", 1) or 1)
            timed_out = "timeout" in type(exc).__name__.lower()
        return {
            "status": "timeout" if timed_out else ("success" if exit_code == 0 else "failure"),
            "returncode": exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "stdout_truncated": False,
            "stderr_truncated": False,
        }

    async def write(self, sandbox_id: str, path: str, content: bytes) -> None:
        sandbox = await self._connect(sandbox_id)
        await sandbox.files.write(path, content)

    async def read(self, sandbox_id: str, path: str) -> bytes:
        sandbox = await self._connect(sandbox_id)
        data = await sandbox.files.read(path, format="bytes")
        return data if isinstance(data, (bytes, bytearray)) else str(data).encode("utf-8")

    async def list(self, sandbox_id: str, path: str) -> list[dict[str, Any]]:
        sandbox = await self._connect(sandbox_id)
        entries = await sandbox.files.list(path)
        out: list[dict[str, Any]] = []
        for entry in entries:
            name = getattr(entry, "name", None) or getattr(entry, "path", "")
            etype = getattr(entry, "type", None)
            is_dir = str(etype).lower().endswith("dir") if etype is not None else False
            out.append({"name": name, "type": "directory" if is_dir else "file"})
        return out

    async def kill(self, sandbox_id: str) -> None:
        try:
            sandbox = await self._connect(sandbox_id)
            await sandbox.kill()
        except SandboxExpired:
            return


def default_runtime() -> SandboxRuntime | None:
    if settings.e2b_api_key:
        return E2BRuntime(settings.e2b_api_key)
    return None
