"""E2B isolated runtime — the real sandbox backing the ``computer.*`` tools.

The :class:`ComputerConnector` talks to this small async interface instead of
running shell on the API host. The real implementation lazily imports the
``e2b`` SDK so the package is only required when the runtime is actually used;
unit tests inject an in-memory fake implementing the same methods.

A "session" maps to one E2B sandbox identified by ``sandbox_id``. Sandboxes live
server-side at E2B and survive across stateless API requests, so every call
reconnects by id rather than holding a live handle.
"""
from __future__ import annotations

from typing import Any, Protocol

from core.config import settings

# Working directory inside every sandbox. Paths the model passes are resolved
# relative to this root and may not escape it.
SANDBOX_ROOT = "/home/user/workspace"


class RuntimeUnavailable(RuntimeError):
    """Raised when no isolated runtime is configured (no E2B key, no fake)."""


class SandboxExpired(RuntimeError):
    """Raised when a sandbox id no longer resolves (expired or killed)."""


class SandboxRuntime(Protocol):
    """The interface the computer connector depends on."""

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
    """Real runtime backed by the e2b SDK. Imported lazily."""

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    async def _connect(self, sandbox_id: str):
        try:
            from e2b import AsyncSandbox  # type: ignore
        except ImportError as exc:  # pragma: no cover - depends on optional dep
            raise RuntimeUnavailable("e2b SDK is not installed") from exc
        try:
            return await AsyncSandbox.connect(sandbox_id, api_key=self._api_key)
        except Exception as exc:  # noqa: BLE001 - SDK raises varied errors on expiry
            raise SandboxExpired(sandbox_id) from exc

    async def create(self, *, timeout_seconds: int, metadata: dict[str, Any]) -> str:
        try:
            from e2b import AsyncSandbox  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise RuntimeUnavailable("e2b SDK is not installed") from exc
        sandbox = await AsyncSandbox.create(
            api_key=self._api_key,
            timeout=timeout_seconds,
            metadata={k: str(v) for k, v in metadata.items() if v is not None},
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
        except Exception as exc:  # noqa: BLE001
            # Non-zero exits and timeouts surface as exceptions in the SDK; read
            # what they carry rather than blowing up the tool call.
            stdout = str(getattr(exc, "stdout", "") or "")
            stderr = str(getattr(exc, "stderr", "") or str(exc))
            exit_code = int(getattr(exc, "exit_code", 1) or 1)
            timed_out = "timeout" in type(exc).__name__.lower()
        status = "timeout" if timed_out else ("success" if exit_code == 0 else "failure")
        return {
            "status": status,
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
    """Return the configured runtime, or None when E2B is not configured."""
    if settings.e2b_api_key:
        return E2BRuntime(settings.e2b_api_key)
    return None
