"""Isolated runtime backing the ``computer.*`` tools.

The computer connector talks to this async interface instead of running shell on
the API host. The real implementation lazily imports the E2B SDK so the package
is only required when an operator configures the runtime; tests inject an
in-memory fake implementing the same methods.
"""
from __future__ import annotations

import shlex
import hashlib
import uuid
from typing import Any, Protocol

from core.config import settings
from core.egress_policy import parse_egress_allowlist

SANDBOX_ROOT = "/home/user/workspace"


class RuntimeUnavailable(RuntimeError):
    """Raised when the isolated runtime cannot be started."""


class SandboxExpired(RuntimeError):
    """Raised when a sandbox id no longer resolves."""


class SandboxRuntime(Protocol):
    async def create(self, *, timeout_seconds: int, metadata: dict[str, Any]) -> str: ...
    async def resume(
        self,
        sandbox_id: str,
        *,
        timeout_seconds: int,
        expected_metadata: dict[str, Any],
    ) -> str: ...
    async def pause(self, sandbox_id: str) -> None: ...
    async def run(self, sandbox_id: str, command: str, *, cwd: str, timeout_seconds: int) -> dict[str, Any]: ...
    async def write(self, sandbox_id: str, path: str, content: bytes) -> None: ...
    async def read(self, sandbox_id: str, path: str) -> bytes: ...
    async def list(self, sandbox_id: str, path: str) -> list[dict[str, Any]]: ...
    async def remove(self, sandbox_id: str, path: str) -> None: ...
    async def keepalive(self, sandbox_id: str, *, timeout_seconds: int) -> None: ...
    async def screenshot(self, sandbox_id: str) -> bytes: ...
    async def desktop_action(self, sandbox_id: str, action: str, payload: dict[str, Any]) -> None: ...
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
    def __init__(
        self,
        api_key: str,
        *,
        template: str | None = None,
        allow_internet_access: bool = False,
        egress_allowlist: list[str] | tuple[str, ...] | str | None = None,
        persistent: bool = False,
        desktop: bool = False,
    ) -> None:
        self._api_key = api_key
        self._template = (template or "").strip() or None
        self._allow_internet_access = bool(allow_internet_access)
        self._egress_allowlist = parse_egress_allowlist(egress_allowlist)
        if self._allow_internet_access and not self._egress_allowlist:
            raise ValueError("Network-enabled E2B profiles require an egress allowlist")
        self._persistent = bool(persistent)
        self._desktop = bool(desktop)
        self._connected: dict[str, Any] = {}

    async def _connect(self, sandbox_id: str):
        cached = self._connected.get(sandbox_id)
        if cached is not None:
            return cached
        try:
            from e2b import AsyncSandbox  # type: ignore
        except ImportError as exc:  # pragma: no cover - optional runtime
            raise RuntimeUnavailable("e2b SDK is not installed") from exc
        try:
            sandbox = await AsyncSandbox.connect(sandbox_id, api_key=self._api_key)
        except Exception as exc:  # noqa: BLE001 - SDK expiry errors vary
            raise SandboxExpired(sandbox_id) from exc
        self._connected[sandbox_id] = sandbox
        return sandbox

    async def create(self, *, timeout_seconds: int, metadata: dict[str, Any]) -> str:
        try:
            from e2b import AsyncSandbox  # type: ignore
        except ImportError as exc:  # pragma: no cover - optional runtime
            raise RuntimeUnavailable("e2b SDK is not installed") from exc
        create_args: dict[str, Any] = {
            "api_key": self._api_key,
            "timeout": timeout_seconds,
            "metadata": {
                key: str(value) for key, value in metadata.items() if value is not None
            },
            # E2B defaults this to true. Pass the setting explicitly so a future
            # SDK update cannot silently weaken the production boundary.
            "allow_internet_access": self._allow_internet_access,
            "secure": True,
        }
        if not self._allow_internet_access:
            # Set both the legacy boolean and the current explicit network
            # policy. E2B documents the boolean as an IPv4 deny rule; adding the
            # IPv6 route prevents a future dual-stack template from bypassing
            # the intended code/data boundary.
            create_args["network"] = {
                "deny_out": ["0.0.0.0/0", "::/0"],
                "allow_public_traffic": False,
            }
            create_args["metadata"]["chronos_egress_policy"] = "deny_all_v1"
        else:
            # E2B documents allow_out as deny-by-default. Keep explicit IPv4
            # and IPv6 deny-all rules too; allowed entries take precedence.
            create_args["network"] = {
                "allow_out": list(self._egress_allowlist),
                "deny_out": ["0.0.0.0/0", "::/0"],
                "allow_public_traffic": False,
            }
            policy_digest = hashlib.sha256(
                "\n".join(self._egress_allowlist).encode("utf-8")
            ).hexdigest()[:16]
            create_args["metadata"]["chronos_egress_policy"] = f"allowlist_v1:{policy_digest}"
        if self._template:
            create_args["template"] = self._template
        if self._persistent:
            create_args["lifecycle"] = {
                "on_timeout": "pause",
                "auto_resume": False,
            }
        if self._desktop:
            create_args["envs"] = {"DISPLAY": ":0"}
        try:
            sandbox = await AsyncSandbox.create(**create_args)
        except Exception as exc:  # noqa: BLE001 - provider exceptions vary
            raise RuntimeUnavailable("e2b sandbox creation failed") from exc
        sandbox_id = getattr(sandbox, "sandbox_id", None) or getattr(sandbox, "id", None)
        if not sandbox_id:
            raise RuntimeUnavailable("e2b did not return a sandbox id")
        sandbox_id = str(sandbox_id)
        self._connected[sandbox_id] = sandbox
        try:
            await sandbox.commands.run(f"mkdir -p {SANDBOX_ROOT}")
            if not self._allow_internet_access:
                await self._attest_deny_egress(sandbox)
            else:
                await self._attest_allowlisted_egress(sandbox)
            if self._desktop:
                await self._start_desktop(sandbox)
        except Exception as exc:  # noqa: BLE001 - provider errors vary
            try:
                await sandbox.kill()
            except Exception:  # noqa: BLE001 - best-effort failed provisioning cleanup
                pass
            self._connected.pop(sandbox_id, None)
            raise RuntimeUnavailable("e2b sandbox failed its security/startup check") from exc
        return sandbox_id

    async def _attest_deny_egress(self, sandbox: Any) -> None:
        """Independently prove a deny-profile cannot open a public TCP socket.

        Provider control-plane configuration is necessary but not sufficient
        evidence. Every new deny-profile sandbox runs this fixed in-sandbox
        probe before Chronos writes user data. Exit 42 means public egress was
        possible and causes immediate sandbox destruction by ``create``.
        """

        probe = (
            "python3 -c \"import socket,sys; s=socket.socket(); s.settimeout(3); "
            "connected=False; "
            "\ntry: s.connect(('1.1.1.1',443)); connected=True"
            "\nexcept OSError: pass"
            "\nfinally: s.close()"
            "\nsys.exit(42 if connected else 0)\""
        )
        try:
            result = await sandbox.commands.run(probe)
        except Exception as exc:  # noqa: BLE001 - SDK non-zero shapes vary
            exit_code = getattr(exc, "exit_code", None)
            if exit_code == 42:
                raise RuntimeUnavailable("e2b deny-egress attestation detected public access") from exc
            raise RuntimeUnavailable("e2b deny-egress attestation could not run") from exc
        exit_code = int(getattr(result, "exit_code", 0) or 0)
        if exit_code == 42:
            raise RuntimeUnavailable("e2b deny-egress attestation detected public access")
        if exit_code != 0:
            raise RuntimeUnavailable("e2b deny-egress attestation could not run")

    async def _attest_allowlisted_egress(self, sandbox: Any) -> None:
        """Prove one allowed domain works and an unlisted domain remains blocked."""

        allowed = next(
            (domain for domain in self._egress_allowlist if not domain.startswith("*.")),
            None,
        )
        if not allowed:
            raise RuntimeUnavailable(
                "e2b egress allowlist needs one exact domain for pre-use attestation"
            )
        # IP literals are forbidden by Chronos allowlist validation, making
        # this stable public endpoint an unambiguously unlisted control probe.
        denied = "1.1.1.1"
        probe = (
            "python3 -c "
            + shlex.quote(
                "import socket,sys\n"
                f"allowed={allowed!r}; denied={denied!r}\n"
                "def opens(host):\n"
                "  try:\n"
                "    s=socket.create_connection((host,443),3); s.close(); return True\n"
                "  except OSError: return False\n"
                "sys.exit(42 if opens(denied) else (43 if not opens(allowed) else 0))"
            )
        )
        try:
            result = await sandbox.commands.run(probe, timeout=12)
        except Exception as exc:  # noqa: BLE001 - SDK non-zero shapes vary
            exit_code = getattr(exc, "exit_code", None)
            if exit_code == 42:
                raise RuntimeUnavailable("e2b allowlist attestation detected unlisted egress") from exc
            if exit_code == 43:
                raise RuntimeUnavailable("e2b allowlist attestation could not reach an allowed domain") from exc
            raise RuntimeUnavailable("e2b allowlist attestation could not run") from exc
        exit_code = int(getattr(result, "exit_code", 0) or 0)
        if exit_code == 42:
            raise RuntimeUnavailable("e2b allowlist attestation detected unlisted egress")
        if exit_code == 43:
            raise RuntimeUnavailable("e2b allowlist attestation could not reach an allowed domain")
        if exit_code != 0:
            raise RuntimeUnavailable("e2b allowlist attestation could not run")

    async def _start_desktop(self, sandbox: Any) -> None:
        width = int(settings.e2b_computer_screen_width)
        height = int(settings.e2b_computer_screen_height)
        startup = (
            "export DISPLAY=:0; "
            "if ! xdpyinfo -display :0 >/dev/null 2>&1; then "
            f"nohup Xvfb :0 -ac -screen 0 {width}x{height}x24 -retro -dpi 96 "
            "-nolisten tcp >/tmp/chronos-xvfb.log 2>&1 & "
            "fi; "
            "for n in $(seq 1 30); do xdpyinfo -display :0 >/dev/null 2>&1 && break; sleep 0.25; done; "
            "xdpyinfo -display :0 >/dev/null 2>&1 || exit 41; "
            "if ! pgrep -f '[x]fce4-session' >/dev/null 2>&1; then "
            "nohup startxfce4 >/tmp/chronos-xfce.log 2>&1 & "
            "fi; "
            "command -v scrot >/dev/null && command -v xdotool >/dev/null"
        )
        result = await sandbox.commands.run(startup, timeout=30)
        if int(getattr(result, "exit_code", 0) or 0) != 0:
            raise RuntimeError("desktop startup command failed")

    async def resume(
        self,
        sandbox_id: str,
        *,
        timeout_seconds: int,
        expected_metadata: dict[str, Any],
    ) -> str:
        try:
            from e2b import AsyncSandbox  # type: ignore
        except ImportError as exc:  # pragma: no cover - runtime dependency
            raise RuntimeUnavailable("e2b SDK is not installed") from exc
        try:
            sandbox = await AsyncSandbox.connect(
                sandbox_id,
                api_key=self._api_key,
                timeout=timeout_seconds,
            )
            info = await sandbox.get_info()
        except Exception as exc:  # noqa: BLE001 - SDK expiry errors vary
            raise SandboxExpired(sandbox_id) from exc
        provider_metadata = dict(getattr(info, "metadata", None) or {})
        required = {
            key: str(value)
            for key, value in expected_metadata.items()
            if value is not None
        }
        if any(str(provider_metadata.get(key, "")) != value for key, value in required.items()):
            try:
                await sandbox.pause()
            except Exception:  # noqa: BLE001 - do not mask ownership failure
                pass
            raise RuntimeUnavailable("e2b sandbox ownership metadata did not match this tenant session")
        self._connected[sandbox_id] = sandbox
        state = getattr(info, "state", None)
        return str(getattr(state, "value", state) or "running")

    async def pause(self, sandbox_id: str) -> None:
        sandbox = await self._connect(sandbox_id)
        try:
            await sandbox.pause()
        except Exception as exc:  # noqa: BLE001 - provider exceptions vary
            raise RuntimeUnavailable("e2b sandbox pause failed") from exc
        finally:
            self._connected.pop(sandbox_id, None)

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
            size = getattr(entry, "size", None)
            out.append(
                {
                    "name": name,
                    "type": "directory" if is_dir else "file",
                    **({"size": int(size)} if size is not None else {}),
                }
            )
        return out

    async def remove(self, sandbox_id: str, path: str) -> None:
        sandbox = await self._connect(sandbox_id)
        await sandbox.files.remove(path)

    async def keepalive(self, sandbox_id: str, *, timeout_seconds: int) -> None:
        """Reconnect and extend a persistent sandbox's provider TTL.

        Repo workspaces call this before each leased operation so any API
        replica can resume the same sandbox. Ephemeral code/data callers never
        use it and retain their create-run-kill lifecycle.
        """

        sandbox = await self._connect(sandbox_id)
        await sandbox.set_timeout(timeout_seconds)

    async def screenshot(self, sandbox_id: str) -> bytes:
        if not self._desktop:
            raise RuntimeUnavailable("this E2B runtime is not backed by a desktop template")
        sandbox = await self._connect(sandbox_id)
        path = f"/tmp/chronos-screen-{uuid.uuid4().hex}.png"
        try:
            result = await sandbox.commands.run(
                f"DISPLAY=:0 scrot --pointer {shlex.quote(path)}",
                timeout=20,
            )
            if int(getattr(result, "exit_code", 0) or 0) != 0:
                raise RuntimeUnavailable("e2b desktop screenshot command failed")
            raw = await sandbox.files.read(path, format="bytes")
            return bytes(raw)
        finally:
            try:
                await sandbox.files.remove(path)
            except Exception:  # noqa: BLE001 - transient screenshot cleanup
                pass

    async def desktop_action(
        self,
        sandbox_id: str,
        action: str,
        payload: dict[str, Any],
    ) -> None:
        if not self._desktop:
            raise RuntimeUnavailable("this E2B runtime is not backed by a desktop template")
        commands: dict[str, str] = {
            "move": f"xdotool mousemove --sync {int(payload['x'])} {int(payload['y'])}",
            "click": (
                f"xdotool mousemove --sync {int(payload['x'])} {int(payload['y'])} "
                f"click {int(payload.get('button', 1))}"
            ),
            "double_click": (
                f"xdotool mousemove --sync {int(payload['x'])} {int(payload['y'])} "
                "click --repeat 2 --delay 120 1"
            ),
            "type": f"xdotool type --clearmodifiers --delay 25 -- {shlex.quote(str(payload['text']))}",
            "key": f"xdotool key --clearmodifiers {shlex.quote(str(payload['key']))}",
            "scroll": (
                f"xdotool click --repeat {int(payload['amount'])} "
                f"{4 if payload.get('direction') == 'up' else 5}"
            ),
            "drag": (
                f"xdotool mousemove --sync {int(payload['x'])} {int(payload['y'])} "
                "mousedown 1 "
                f"mousemove --sync {int(payload['to_x'])} {int(payload['to_y'])} mouseup 1"
            ),
        }
        command = commands.get(action)
        if not command:
            raise ValueError(f"Unsupported desktop action: {action}")
        result = await self.run(
            sandbox_id,
            f"DISPLAY=:0 {command}",
            cwd=SANDBOX_ROOT,
            timeout_seconds=20,
        )
        if result["status"] != "success":
            raise RuntimeUnavailable("e2b desktop input command failed")

    async def kill(self, sandbox_id: str) -> None:
        try:
            sandbox = await self._connect(sandbox_id)
            await sandbox.kill()
        except SandboxExpired:
            return
        finally:
            self._connected.pop(sandbox_id, None)


def _configured_runtime(
    *,
    allow_internet_access: bool,
    template: str | None = None,
    persistent: bool = False,
    desktop: bool = False,
    egress_allowlist: list[str] | tuple[str, ...] | str | None = None,
) -> SandboxRuntime | None:
    api_key = settings.e2b_api_key.strip()
    if allow_internet_access and not parse_egress_allowlist(egress_allowlist):
        return None
    if api_key:
        return E2BRuntime(
            api_key,
            template=template if template is not None else settings.e2b_template_id,
            allow_internet_access=allow_internet_access,
            egress_allowlist=egress_allowlist,
            persistent=persistent,
            desktop=desktop,
        )
    return None


def default_runtime() -> SandboxRuntime | None:
    """Return the fail-closed runtime for arbitrary code, data, and skills.

    This factory intentionally ignores every network-enabling setting. Callers
    handling untrusted Python or bundled skill scripts cannot inherit the cloud
    computer's web-capable profile by accident.
    """

    return _configured_runtime(allow_internet_access=False)


def computer_runtime(
    *,
    allow_internet_access: bool | None = None,
    egress_allowlist: list[str] | tuple[str, ...] | str | None = None,
) -> SandboxRuntime | None:
    """Return the separately configured runtime for ``computer.*`` sessions."""

    return _configured_runtime(
        allow_internet_access=(
            settings.e2b_computer_allow_internet_access
            if allow_internet_access is None
            else bool(allow_internet_access)
        ),
        egress_allowlist=(
            egress_allowlist
            if egress_allowlist is not None
            else settings.e2b_computer_egress_allowlist
        ),
        template=settings.e2b_computer_template_id.strip() or "desktop",
        persistent=True,
        desktop=True,
    )


def repo_runtime() -> SandboxRuntime | None:
    """Return the separately configured persistent repo-workspace runtime."""

    if not settings.e2b_repo_enabled:
        return None
    api_key = settings.e2b_api_key.strip()
    if not api_key:
        return None
    if settings.e2b_repo_allow_internet_access and not parse_egress_allowlist(
        settings.e2b_repo_egress_allowlist
    ):
        return None
    return E2BRuntime(
        api_key,
        template=settings.e2b_repo_template_id,
        allow_internet_access=settings.e2b_repo_allow_internet_access,
        egress_allowlist=settings.e2b_repo_egress_allowlist,
        persistent=True,
    )
