from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import posixpath
import re
import resource
import shlex
import shutil
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any

from sqlalchemy import func, insert, select, update

from core import audit
from core.artifacts import save_artifact
from core.config import settings
from core.db import engine, reflect_table
from core.models import ToolResult
from core.workspace import jailed_path
from core.execution_boundary import api_host_execution_allowed
from core.egress_policy import normalize_consent_domains
from core.redis import redis_client
from connectors.e2b_runtime import (
    SANDBOX_ROOT,
    RuntimeUnavailable,
    SandboxExpired,
    SandboxRuntime,
    computer_runtime,
    remote_path,
)

MAX_READ_BYTES = 512_000
MAX_WRITE_BYTES = 512_000
MAX_OUTPUT_BYTES = 128_000
MAX_TIMEOUT_SECONDS = 30
DEFAULT_TIMEOUT_SECONDS = 10
CLOUD_MAX_TIMEOUT_SECONDS = 600
CLOUD_DEFAULT_TIMEOUT_SECONDS = 60
COMMAND_BRIDGE_TTL_SECONDS = 300
SCREENSHOT_MAX_BYTES = 8 * 1024 * 1024
COMPUTER_CAPABILITIES = {"terminal", "files", "packages", "desktop", "network"}
ACTIVE_SESSION_STATUSES = {"active", "paused", "provisioning"}
DESKTOP_KEY_ALIASES = {
    "enter": "Return",
    "return": "Return",
    "backspace": "BackSpace",
    "escape": "Escape",
    "esc": "Escape",
    "delete": "Delete",
    "tab": "Tab",
    "space": "space",
    "up": "Up",
    "down": "Down",
    "left": "Left",
    "right": "Right",
    "page_up": "Page_Up",
    "page_down": "Page_Down",
    "ctrl": "Control_L",
    "control": "Control_L",
    "alt": "Alt_L",
    "shift": "Shift_L",
    "cmd": "Super_L",
    "super": "Super_L",
}
_QUOTA_RELEASE_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
else
    return 0
end
"""
FORBIDDEN_COMMAND_MARKERS = (
    " rm -rf ",
    "mkfs",
    "diskutil erase",
    "shutdown",
    "reboot",
    ":(){",
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp() -> str:
    return _now().isoformat()


def _uuid_or_none(value: Any) -> str | None:
    if not value:
        return None
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError):
        return None


def _public_session(session: dict[str, Any]) -> dict[str, Any]:
    safe = dict(session)
    safe.pop("environment", None)
    editor_state = dict(safe.pop("editor_state", {}) or {})
    consent = dict(editor_state.get("consent") or {})
    safe["capabilities"] = list(consent.get("capabilities") or [])
    safe["allowed_egress_domains"] = list(consent.get("allowed_egress_domains") or [])
    safe["expires_at"] = consent.get("expires_at")
    return safe


def _public_grant(grant: dict[str, Any]) -> dict[str, Any]:
    safe = dict(grant)
    if safe.get("device_id"):
        safe.pop("folder_path", None)
        safe["display_name"] = safe.get("folder_display_name") or "Folder"
    return safe


def _coerce_session(row: dict[str, Any]) -> dict[str, Any]:
    if row.get("id") is not None:
        row["id"] = str(row["id"])
    row.setdefault("environment", {})
    row.setdefault("resource_limits", {})
    row.setdefault("network_policy", {})
    row.setdefault("history", [])
    return row


def _tenant_marker(organization_id: str) -> str:
    return hashlib.sha256(organization_id.encode("utf-8")).hexdigest()


def _parse_timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value or "").strip()
        if raw.endswith("Z"):
            raw = f"{raw[:-1]}+00:00"
        parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        raise ValueError("expires_at must include a timezone")
    return parsed.astimezone(timezone.utc)


def _normalize_consent(value: Any, *, purpose: str) -> dict[str, Any]:
    consent = dict(value or {}) if isinstance(value, dict) else {}
    if consent.get("confirmed_by_user") is not True:
        raise PermissionError("Cloud computer creation requires explicit user confirmation")
    consent_purpose = str(consent.get("purpose") or "").strip()
    if not purpose or consent_purpose != purpose:
        raise ValueError("Cloud computer consent purpose must match the requested purpose")
    expires_at = _parse_timestamp(consent.get("expires_at"))
    now = _now()
    remaining = (expires_at - now).total_seconds()
    if remaining < 60:
        raise ValueError("Cloud computer consent must remain valid for at least 60 seconds")
    if remaining > settings.e2b_computer_max_session_seconds:
        raise ValueError(
            "Cloud computer consent exceeds E2B_COMPUTER_MAX_SESSION_SECONDS"
        )
    requested = consent.get("capabilities")
    if not isinstance(requested, list) or not requested:
        raise ValueError("Cloud computer consent must list at least one capability")
    capabilities = sorted({str(item).strip().lower() for item in requested})
    invalid = set(capabilities) - COMPUTER_CAPABILITIES
    if invalid:
        raise ValueError(f"Unsupported cloud computer capabilities: {', '.join(sorted(invalid))}")
    if "packages" in capabilities and "network" not in capabilities:
        raise ValueError("Package installation requires the network capability")
    if "network" in capabilities and not settings.e2b_computer_allow_internet_access:
        raise PermissionError("Cloud computer network access is disabled by organization policy")
    allowed_egress_domains: list[str] = []
    if "network" in capabilities:
        allowed_egress_domains = normalize_consent_domains(
            consent.get("allowed_egress_domains"),
            policy=settings.e2b_computer_egress_allowlist,
        )
    elif consent.get("allowed_egress_domains"):
        raise ValueError("Egress domains require the network capability")
    return {
        "purpose": purpose,
        "capabilities": capabilities,
        "expires_at": expires_at.isoformat(),
        "confirmed_by_user": True,
        "confirmed_at": now.isoformat(),
        "allowed_egress_domains": allowed_egress_domains,
    }


def _session_consent(session: dict[str, Any]) -> dict[str, Any]:
    return dict((session.get("editor_state") or {}).get("consent") or {})


def _coerce_grant(row: dict[str, Any]) -> dict[str, Any]:
    if row.get("id") is not None:
        row["id"] = str(row["id"])
    row.setdefault("allowed_commands", [])
    row.setdefault("allowed_apps", [])
    return row


def _set_command_limits() -> None:
    limits = [
        (resource.RLIMIT_CPU, (20, 20)),
        (resource.RLIMIT_AS, (1024 * 1024 * 1024, 1024 * 1024 * 1024)),
        (resource.RLIMIT_FSIZE, (25 * 1024 * 1024, 25 * 1024 * 1024)),
    ]
    for limit, value in limits:
        try:
            resource.setrlimit(limit, value)
        except (OSError, ValueError):
            continue


def _validate_command(command: str) -> None:
    normalized = f" {command.lower().strip()} "
    if not command.strip():
        raise ValueError("command is required")
    if any(marker in normalized for marker in FORBIDDEN_COMMAND_MARKERS):
        raise ValueError("computer command rejected by safety policy")


def _timeout(value: Any) -> int:
    try:
        parsed = int(value or DEFAULT_TIMEOUT_SECONDS)
    except (TypeError, ValueError):
        parsed = DEFAULT_TIMEOUT_SECONDS
    return max(1, min(parsed, MAX_TIMEOUT_SECONDS))


def _cloud_timeout(value: Any) -> int:
    try:
        parsed = int(value or CLOUD_DEFAULT_TIMEOUT_SECONDS)
    except (TypeError, ValueError):
        parsed = CLOUD_DEFAULT_TIMEOUT_SECONDS
    return max(1, min(parsed, CLOUD_MAX_TIMEOUT_SECONDS))


async def _run_shell(command: str, *, cwd: Path, timeout_seconds: int) -> dict[str, Any]:
    _validate_command(command)
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(cwd),
        "TMPDIR": str(cwd / ".tmp"),
        "PYTHONNOUSERSITE": "1",
    }
    (cwd / ".tmp").mkdir(exist_ok=True)
    # Non-login shell (``-c`` not ``-lc``): the environment is set explicitly
    # above, so sourcing login profiles (/etc/profile, ~/.profile) only risks
    # polluting command stdout with shell-init noise and non-deterministic output.
    process = await asyncio.create_subprocess_exec(
        "/bin/sh",
        "-c",
        command,
        cwd=str(cwd),
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        preexec_fn=_set_command_limits if os.name == "posix" else None,
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
    return {
        "status": "timeout" if timed_out else ("success" if process.returncode == 0 else "failure"),
        "returncode": process.returncode,
        "stdout": out,
        "stderr": err,
        "stdout_truncated": len(stdout) > MAX_OUTPUT_BYTES,
        "stderr_truncated": len(stderr) > MAX_OUTPUT_BYTES,
    }


class ComputerConnector:
    def __init__(self) -> None:
        self._sessions: dict[str, dict[str, Any]] = {}
        self._events: dict[str, list[dict[str, Any]]] = {}
        self._grants: dict[str, dict[str, Any]] = {}
        self._local_events: dict[str, list[dict[str, Any]]] = {}
        self._runtime: SandboxRuntime | None = None

    def _get_runtime(
        self,
        *,
        allow_internet_access: bool | None = None,
        egress_allowlist: list[str] | None = None,
    ) -> SandboxRuntime | None:
        return (
            self._runtime
            if self._runtime is not None
            else computer_runtime(
                allow_internet_access=allow_internet_access,
                egress_allowlist=egress_allowlist,
            )
        )

    @staticmethod
    def _sandbox_id(session: dict[str, Any]) -> str:
        sandbox_id = (session.get("environment") or {}).get("sandbox_id")
        if not sandbox_id:
            raise SandboxExpired(str(session.get("id") or ""))
        return str(sandbox_id)

    @staticmethod
    def _provider_metadata(session: dict[str, Any]) -> dict[str, Any]:
        metadata = dict((session.get("environment") or {}).get("metadata") or {})
        required = {"chronos_tenant", "chronos_session"}
        if not required <= metadata.keys():
            raise RuntimeUnavailable(
                "cloud computer session lacks tenant-bound provider metadata; create a new session"
            )
        return metadata

    @staticmethod
    def _rel(full: str) -> str:
        rel = full[len(SANDBOX_ROOT):].lstrip("/")
        return rel or "."

    async def execute(self, tool: str, args: dict[str, Any]) -> ToolResult:
        args.pop("__connector_tier", None)
        org_id = str(args.pop("__org_id", settings.org_id) or settings.org_id)
        task_id = str(args.pop("__task_id", "") or "")
        member_id = str(args.pop("__member_id", "") or "chronos")
        provider, action = tool.split(".", 1)
        if provider == "computer":
            return await self._execute_cloud(
                action,
                args,
                organization_id=org_id,
                member_id=member_id,
                task_id=task_id or None,
            )
        if provider == "local_computer":
            if not api_host_execution_allowed():
                return await self._execute_local_bridge(
                    action,
                    args,
                    organization_id=org_id,
                    member_id=member_id,
                    task_id=task_id or None,
                )
            return await self._execute_local(action, args, organization_id=org_id, task_id=task_id or None)
        raise ValueError(f"Unknown computer tool: {tool}")

    async def _execute_cloud(
        self,
        action: str,
        args: dict[str, Any],
        *,
        organization_id: str,
        member_id: str,
        task_id: str | None,
    ) -> ToolResult:
        if action == "create_session":
            purpose = str(args.get("purpose") or "").strip()
            consent = _normalize_consent(args.get("consent"), purpose=purpose)
            runtime = self._get_runtime(
                allow_internet_access="network" in consent["capabilities"],
                egress_allowlist=consent["allowed_egress_domains"],
            )
            if runtime is None:
                return self._runtime_not_configured()
            try:
                session = await self.create_session(
                    runtime=runtime,
                    organization_id=organization_id,
                    member_id=member_id,
                    task_id=task_id,
                    purpose=purpose,
                    consent=consent,
                )
            except RuntimeUnavailable as exc:
                return self._runtime_unavailable(str(exc))
            return ToolResult(data={"session": session}, summary="Cloud computer session created")

        runtime = self._get_runtime()
        if runtime is None:
            return self._runtime_not_configured()
        session_id = str(args.get("session_id") or "").strip()
        if not session_id:
            raise ValueError("session_id is required; create a consented computer session first")
        session = await self._load_session(session_id, organization_id)
        try:
            if action == "cancel_session":
                return await self._cancel_session(runtime, session, member_id=member_id)
            if action == "pause_session":
                return await self._pause_session(runtime, session)
            if action == "resume_session":
                await self._prepare_session(runtime, session)
                await self._record_event(session, "computer_session_resumed", {})
                return ToolResult(
                    data={"status": "active", "session": _public_session(session)},
                    summary="Cloud computer session resumed",
                )
            if action == "list_files":
                await self._prepare_session(runtime, session, capability="files")
                return await self._cloud_list(runtime, session, args)
            if action == "read_file":
                await self._prepare_session(runtime, session, capability="files")
                return await self._cloud_read(runtime, session, args)
            if action == "write_file":
                await self._prepare_session(runtime, session, capability="files")
                return await self._cloud_write(runtime, session, args)
            if action == "exec":
                await self._prepare_session(runtime, session, capability="terminal")
                return await self._cloud_exec(runtime, session, args)
            if action == "install_package":
                await self._prepare_session(runtime, session, capability="packages")
                command = self._package_install_command(args)
                return await self._cloud_exec(
                    runtime,
                    session,
                    {"command": command, "timeout_seconds": args.get("timeout_seconds", 120)},
                    event_type="computer_package_install",
                )
            if action == "screenshot":
                await self._prepare_session(runtime, session, capability="desktop")
                return await self._cloud_screenshot(runtime, session)
            if action == "input":
                await self._prepare_session(runtime, session, capability="desktop")
                return await self._cloud_input(runtime, session, args)
            if action == "export_artifact":
                await self._prepare_session(runtime, session, capability="files")
                return await self._cloud_export(runtime, session, args)
        except RuntimeUnavailable as exc:
            return self._runtime_unavailable(str(exc))
        except SandboxExpired:
            await self._mark_session_expired(session)
            return ToolResult(
                data={
                    "status": "expired",
                    "reason": "This cloud computer session has expired. Create a new session and re-copy any files you need.",
                    "session": _public_session(session),
                },
                summary="Cloud computer session expired",
            )
        raise ValueError(f"Unknown computer tool: computer.{action}")

    @staticmethod
    def _runtime_not_configured() -> ToolResult:
        return ToolResult(
            data={
                "status": "unavailable",
                "reason": (
                    "No isolated desktop runtime is configured. Set E2B_API_KEY and "
                    "E2B_COMPUTER_TEMPLATE_ID to a hardened E2B desktop template."
                ),
            },
            summary="Cloud computer runtime not configured",
        )

    def _runtime_unavailable(self, reason: str) -> ToolResult:
        return ToolResult(
            data={
                "status": "unavailable",
                "reason": f"Cloud computer isolated runtime is unavailable: {reason}",
            },
            summary="Cloud computer runtime unavailable",
        )

    async def _mark_session_expired(self, session: dict[str, Any]) -> None:
        session["status"] = "expired"
        session["closed_at"] = _now()
        await self._save_session(session)

    async def _execute_local(
        self, action: str, args: dict[str, Any], *, organization_id: str, task_id: str | None
    ) -> ToolResult:
        if action == "grant":
            grant = await self.create_local_grant(
                organization_id=organization_id,
                member_id=str(args.get("member_id") or "chronos"),
                folder_path=str(args.get("folder_path") or ""),
                purpose=str(args.get("purpose") or "local computer task"),
                task_id=task_id,
            )
            return ToolResult(data={"grant": grant}, summary="Local computer folder granted")
        grant = await self._load_grant(str(args.get("grant_id") or ""), organization_id)
        root = Path(grant["folder_path"]).resolve()
        if action == "list_files":
            return await self._local_list(grant, root, args)
        if action == "read_file":
            return await self._local_read(grant, root, args)
        if action == "exec":
            return await self._local_exec(grant, root, args)
        if action == "open_app":
            return await self._local_open_app(grant, args)
        if action == "revoke":
            revoked = await self.revoke_local_grant(grant["id"], organization_id=organization_id)
            return ToolResult(data={"grant": revoked}, summary="Local computer grant revoked")
        raise ValueError(f"Unknown local computer tool: local_computer.{action}")

    @staticmethod
    def _bridge_relative_path(value: Any, *, required: bool = False) -> str:
        raw = str(value or "").strip()
        if not raw:
            if required:
                raise ValueError("path is required")
            return "."
        if "\\" in raw or any(ord(character) < 32 for character in raw):
            raise PermissionError("Desktop bridge paths must be relative to the folder grant")
        path = PurePosixPath(raw)
        if path.is_absolute() or ".." in path.parts:
            raise PermissionError("Desktop bridge paths must stay inside the folder grant")
        normalized = str(path)
        return normalized if normalized not in {"", "/"} else "."

    async def _execute_local_bridge(
        self,
        action: str,
        args: dict[str, Any],
        *,
        organization_id: str,
        member_id: str,
        task_id: str | None,
    ) -> ToolResult:
        """Route production local-computer actions to a paired client device.

        The API stores only an opaque client grant id and never receives a Mac
        path.  Execution remains subject to the broker's existing approval gate;
        this method only creates signed, leased commands after that gate passes.
        """

        from core.desktop_bridge import DesktopBridgeError, desktop_bridge

        if action == "grant":
            devices = await desktop_bridge.list_devices(
                organization_id=organization_id, member_id=member_id
            )
            active_devices = [device for device in devices if device["status"] == "active"]
            return ToolResult(
                data={
                    "status": "requires_device_grant",
                    "reason": (
                        "Choose a folder in the Chronos desktop app. The app keeps the absolute "
                        "path locally and registers only an opaque grant id and display name."
                    ),
                    "paired_devices": [
                        {"id": device["id"], "name": device["name"], "platform": device["platform"]}
                        for device in active_devices
                    ],
                    "host_execution": False,
                    "execution_boundary": "authenticated_desktop_device",
                },
                summary="Local folder authorization is required in the desktop app",
            )

        grant_id = str(args.get("grant_id") or "")
        if not grant_id:
            raise DesktopBridgeError("grant_id_required", "grant_id is required")

        payload: dict[str, Any]
        if action == "list_files":
            command_type = "list_files"
            payload = {"path": self._bridge_relative_path(args.get("path"))}
        elif action == "read_file":
            command_type = "read_file"
            payload = {"path": self._bridge_relative_path(args.get("path"), required=True)}
        elif action == "exec":
            command_type = "exec"
            command = str(args.get("command") or "")
            _validate_command(command)
            payload = {"command": command, "timeout_seconds": _timeout(args.get("timeout_seconds"))}
        elif action == "open_app":
            command_type = "open_app"
            app = str(args.get("app") or "").strip()
            if not app or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._+()'-]{0,199}", app):
                raise ValueError("app must be a safe application name or bundle identifier")
            payload = {"app": app}
        elif action == "revoke":
            command_type = "revoke_grant"
            payload = {}
        else:
            raise ValueError(f"Unknown local computer tool: local_computer.{action}")

        queued = await desktop_bridge.enqueue(
            organization_id=organization_id,
            member_id=member_id,
            grant_id=grant_id,
            command_type=command_type,
            payload={**payload, "task_id": task_id},
            task_id=task_id,
            ttl_seconds=(
                int(payload.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS) + 120
                if command_type == "exec"
                else COMMAND_BRIDGE_TTL_SECONDS
            ),
        )
        if action == "revoke":
            revoked = await desktop_bridge.revoke_grant_from_web(
                organization_id=organization_id,
                member_id=member_id,
                grant_id=grant_id,
                actor_id=member_id,
                notify_device=False,
            )
        else:
            revoked = None

        default_bridge_wait = (
            min(float(payload.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS) + 10.0, 60.0)
            if command_type == "exec"
            else 15.0
        )
        try:
            wait_seconds = float(args.get("bridge_wait_seconds", default_bridge_wait))
        except (TypeError, ValueError):
            wait_seconds = default_bridge_wait
        state = await desktop_bridge.wait_for_result(
            queued["command_id"], timeout_seconds=max(0.0, min(wait_seconds, 60.0))
        )
        boundary = {
            "command_id": queued["command_id"],
            "device_id": queued["device_id"],
            "host_execution": False,
            "execution_boundary": "authenticated_desktop_device",
        }
        if state["status"] == "succeeded":
            result = state.get("result")
            data = dict(result) if isinstance(result, dict) else {"result": result}
            data.update(boundary, status=str(data.get("status") or "success"))
            return ToolResult(data=data, summary=f"Desktop device {command_type} succeeded")
        if state["status"] == "failed":
            result = state.get("result")
            data = dict(result) if isinstance(result, dict) else {"result": result}
            data.update(
                boundary,
                status="failure",
                error_code=state.get("error_code"),
            )
            return ToolResult(data=data, summary=f"Desktop device {command_type} failed")
        if state["status"] in {"expired", "cancelled"}:
            return ToolResult(
                data={**boundary, **state, "status": state["status"]},
                summary=f"Desktop device {command_type} {state['status']}",
            )
        return ToolResult(
            data={
                **boundary,
                "status": "queued",
                "reason": (
                    "The authenticated desktop device has not returned a result yet. "
                    "The durable command remains queued or leased until its expiry."
                ),
                "expires_at": queued["expires_at"],
                **({"grant": revoked} if revoked else {}),
            },
            summary=f"Desktop device {command_type} queued",
        )

    async def create_session(
        self,
        *,
        runtime: SandboxRuntime,
        organization_id: str,
        member_id: str,
        task_id: str | None,
        purpose: str,
        consent: dict[str, Any],
    ) -> dict[str, Any]:
        async with self._quota_admission(organization_id):
            sessions = await self.list_sessions(organization_id=organization_id)
            live = [session for session in sessions if session.get("status") in ACTIVE_SESSION_STATUSES]
            member_live = [
                session for session in live if str(session.get("member_id") or "") == member_id
            ]
            if len(member_live) >= settings.e2b_computer_max_active_per_member:
                raise RuntimeUnavailable("cloud computer per-member active-session quota reached")
            if len(live) >= settings.e2b_computer_max_active_per_org:
                raise RuntimeUnavailable("cloud computer organization active-session quota reached")
            session_id = str(uuid.uuid4())
            metadata = {
                "chronos_tenant": _tenant_marker(organization_id),
                "chronos_session": session_id,
            }
            sandbox_id = await runtime.create(
                timeout_seconds=settings.e2b_computer_idle_timeout_seconds,
                metadata=metadata,
            )
        session = {
            "id": session_id,
            "organization_id": organization_id,
            "region": settings.region,
            "task_id": task_id,
            "member_id": member_id,
            "status": "active",
            "purpose": purpose,
            "workspace_path": SANDBOX_ROOT,
            "browser_session_id": None,
            "editor_state": {"consent": consent},
            "network_policy": {
                "mode": "allowlist" if "network" in consent["capabilities"] else "deny_egress",
                "provider": "e2b",
                "allowed_domains": consent["allowed_egress_domains"],
            },
            "resource_limits": {
                "command_timeout_seconds": CLOUD_MAX_TIMEOUT_SECONDS,
                "idle_pause_seconds": settings.e2b_computer_idle_timeout_seconds,
                "hard_expiry_seconds": settings.e2b_computer_max_session_seconds,
                "screen_width": settings.e2b_computer_screen_width,
                "screen_height": settings.e2b_computer_screen_height,
            },
            "environment": {
                "sandbox_id": sandbox_id,
                "runtime": "e2b",
                "metadata": metadata,
                "resumable": True,
                "desktop": True,
            },
            "history": [],
            "created_at": _now(),
            "updated_at": _now(),
            "closed_at": None,
        }
        try:
            await self._save_session(session)
            await self._record_event(
                session,
                "computer_session_created",
                {
                    "purpose": purpose,
                    "runtime": "e2b",
                    "capabilities": consent["capabilities"],
                    "expires_at": consent["expires_at"],
                    "allowed_egress_domains": consent["allowed_egress_domains"],
                },
            )
        except Exception:
            await runtime.kill(sandbox_id)
            raise
        return _public_session(session)

    @asynccontextmanager
    async def _quota_admission(self, organization_id: str):
        key = f"lock:computer-quota:{hashlib.sha256(organization_id.encode()).hexdigest()}"
        token = uuid.uuid4().hex
        acquired = False
        try:
            acquired = bool(await redis_client.set(key, token, nx=True, ex=60))
        except Exception as exc:  # noqa: BLE001 - Redis is optional outside production
            if settings.is_production:
                raise RuntimeUnavailable("cloud computer quota coordination is unavailable") from exc
            acquired = True
        if not acquired:
            raise RuntimeUnavailable("cloud computer quota admission is busy; retry")
        try:
            yield
        finally:
            try:
                await redis_client.eval(_QUOTA_RELEASE_LUA, 1, key, token)
            except Exception:  # noqa: BLE001 - lock TTL is the cleanup backstop
                pass

    async def _prepare_session(
        self,
        runtime: SandboxRuntime,
        session: dict[str, Any],
        *,
        capability: str | None = None,
    ) -> None:
        if session.get("status") not in {"active", "paused"}:
            raise PermissionError(f"cloud computer session is {session.get('status') or 'closed'}")
        consent = _session_consent(session)
        expires_at = _parse_timestamp(consent.get("expires_at"))
        if expires_at <= _now():
            try:
                await runtime.kill(self._sandbox_id(session))
            finally:
                session.update({"status": "expired", "closed_at": _now(), "updated_at": _now()})
                await self._save_session(session)
                await self._record_event(session, "computer_session_expired", {})
            raise PermissionError("cloud computer consent has expired")
        capabilities = set(consent.get("capabilities") or [])
        if capability and capability not in capabilities:
            raise PermissionError(f"cloud computer consent does not allow {capability}")
        await runtime.resume(
            self._sandbox_id(session),
            timeout_seconds=settings.e2b_computer_idle_timeout_seconds,
            expected_metadata=self._provider_metadata(session),
        )
        was_paused = session.get("status") == "paused"
        session.update({"status": "active", "updated_at": _now()})
        await self._save_session(session)
        if was_paused:
            await self._record_event(session, "computer_session_resumed", {"source": "activity"})

    async def _pause_session(
        self,
        runtime: SandboxRuntime,
        session: dict[str, Any],
    ) -> ToolResult:
        await self._prepare_session(runtime, session)
        await runtime.pause(self._sandbox_id(session))
        session.update({"status": "paused", "updated_at": _now()})
        await self._save_session(session)
        await self._record_event(session, "computer_session_paused", {"source": "user"})
        return ToolResult(
            data={"status": "paused", "session": _public_session(session)},
            summary="Cloud computer session paused",
        )

    async def _cancel_session(
        self,
        runtime: SandboxRuntime,
        session: dict[str, Any],
        *,
        member_id: str,
    ) -> ToolResult:
        if session.get("status") == "cancelled":
            return ToolResult(
                data={"status": "cancelled", "session": _public_session(session)},
                summary="Cloud computer session already cancelled",
            )
        try:
            await runtime.resume(
                self._sandbox_id(session),
                timeout_seconds=60,
                expected_metadata=self._provider_metadata(session),
            )
            await runtime.kill(self._sandbox_id(session))
        except SandboxExpired:
            pass
        session.update({"status": "cancelled", "closed_at": _now(), "updated_at": _now()})
        editor_state = dict(session.get("editor_state") or {})
        editor_state["cancelled_by"] = member_id
        session["editor_state"] = editor_state
        await self._save_session(session)
        await self._record_event(session, "computer_session_cancelled", {"member_id": member_id})
        return ToolResult(
            data={"status": "cancelled", "session": _public_session(session)},
            summary="Cloud computer session cancelled and destroyed",
        )

    async def _cloud_list(self, runtime: SandboxRuntime, session: dict[str, Any], args: dict[str, Any]) -> ToolResult:
        parent = remote_path(str(args.get("path") or "."))
        children = await runtime.list(self._sandbox_id(session), parent)
        entries = [
            {
                "path": self._rel(f"{parent.rstrip('/')}/{child['name']}"),
                "type": child["type"],
                "size": None,
            }
            for child in children[:200]
        ]
        await self._record_event(session, "computer_files_listed", {"path": self._rel(parent), "count": len(entries)})
        return ToolResult(data={"session": _public_session(session), "entries": entries}, summary=f"Listed {len(entries)} computer files")

    async def _cloud_read(self, runtime: SandboxRuntime, session: dict[str, Any], args: dict[str, Any]) -> ToolResult:
        path = remote_path(str(args.get("path") or ""))
        raw = await runtime.read(self._sandbox_id(session), path)
        await self._record_event(session, "computer_file_read", {"path": self._rel(path), "bytes": len(raw)})
        return ToolResult(
            data={
                "session": _public_session(session),
                "path": self._rel(path),
                "content": raw[:MAX_READ_BYTES].decode("utf-8", errors="replace"),
                "bytes": len(raw),
                "truncated": len(raw) > MAX_READ_BYTES,
            },
            summary=f"Read computer file {self._rel(path)}",
        )

    async def _cloud_write(self, runtime: SandboxRuntime, session: dict[str, Any], args: dict[str, Any]) -> ToolResult:
        path = remote_path(str(args.get("path") or ""))
        raw = str(args.get("content") or "").encode("utf-8")
        if len(raw) > MAX_WRITE_BYTES:
            raise ValueError(f"computer.write_file payload exceeds {MAX_WRITE_BYTES} bytes")
        sandbox_id = self._sandbox_id(session)
        parent = posixpath.dirname(path)
        if parent and parent != SANDBOX_ROOT:
            await runtime.run(sandbox_id, f"mkdir -p {shlex.quote(parent)}", cwd=SANDBOX_ROOT, timeout_seconds=15)
        await runtime.write(sandbox_id, path, raw)
        await self._record_event(session, "computer_file_written", {"path": self._rel(path), "bytes": len(raw)})
        return ToolResult(
            data={"session": _public_session(session), "path": self._rel(path), "bytes": len(raw)},
            summary=f"Wrote computer file {self._rel(path)}",
        )

    async def _cloud_exec(
        self,
        runtime: SandboxRuntime,
        session: dict[str, Any],
        args: dict[str, Any],
        *,
        event_type: str = "computer_command",
    ) -> ToolResult:
        command = str(args.get("command") or "")
        result = await runtime.run(
            self._sandbox_id(session),
            command,
            cwd=SANDBOX_ROOT,
            timeout_seconds=_cloud_timeout(args.get("timeout_seconds")),
        )
        payload = {
            "command": command,
            "status": result["status"],
            "returncode": result["returncode"],
            "stdout_bytes": len(result["stdout"].encode("utf-8")),
            "stderr_bytes": len(result["stderr"].encode("utf-8")),
        }
        await self._record_event(session, event_type, payload)
        return ToolResult(
            data={**result, "session": _public_session(session), "workspace": SANDBOX_ROOT},
            summary=f"Computer command {result['status']}",
        )

    def _package_install_command(self, args: dict[str, Any]) -> str:
        manager = str(args.get("manager") or "pip").lower()
        package = str(args.get("package") or "").strip()
        if not package:
            raise ValueError("package is required")
        if any(ch in package for ch in ";&|`$<>"):
            raise ValueError("package name rejected by safety policy")
        if manager == "pip":
            return f"python3 -m pip install --target .packages {package}"
        if manager == "npm":
            return f"npm install --prefix . {package}"
        raise ValueError("package manager must be pip or npm")

    async def _cloud_screenshot(
        self,
        runtime: SandboxRuntime,
        session: dict[str, Any],
    ) -> ToolResult:
        raw = await runtime.screenshot(self._sandbox_id(session))
        if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
            raise RuntimeUnavailable("E2B desktop returned an invalid screenshot")
        if len(raw) > SCREENSHOT_MAX_BYTES:
            raise RuntimeUnavailable("E2B desktop screenshot exceeded the response limit")
        await self._record_event(
            session,
            "computer_screenshot",
            {"status": "success", "bytes": len(raw)},
        )
        return ToolResult(
            data={
                "status": "success",
                "screenshot_data_url": (
                    "data:image/png;base64," + base64.b64encode(raw).decode("ascii")
                ),
                "width": settings.e2b_computer_screen_width,
                "height": settings.e2b_computer_screen_height,
                "session": _public_session(session),
            },
            summary="Captured cloud computer screenshot",
        )

    async def _cloud_input(
        self,
        runtime: SandboxRuntime,
        session: dict[str, Any],
        args: dict[str, Any],
    ) -> ToolResult:
        action = str(args.get("action") or "").strip().lower()
        if action not in {"move", "click", "double_click", "type", "key", "scroll", "drag"}:
            raise ValueError("Unsupported cloud computer input action")
        payload: dict[str, Any] = {}

        def coordinate(name: str) -> int:
            try:
                value = int(args.get(name))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{name} is required") from exc
            limit = (
                settings.e2b_computer_screen_width
                if name in {"x", "to_x"}
                else settings.e2b_computer_screen_height
            )
            if not 0 <= value < limit:
                raise ValueError(f"{name} must be between 0 and {limit - 1}")
            return value

        if action in {"move", "click", "double_click", "drag"}:
            payload.update(x=coordinate("x"), y=coordinate("y"))
        if action == "drag":
            payload.update(to_x=coordinate("to_x"), to_y=coordinate("to_y"))
        if action == "click":
            button = str(args.get("button") or "left").lower()
            buttons = {"left": 1, "middle": 2, "right": 3}
            if button not in buttons:
                raise ValueError("button must be left, middle, or right")
            payload["button"] = buttons[button]
        if action == "type":
            text = str(args.get("text") or "")
            if not text or len(text) > 4000 or any(ord(char) == 0 for char in text):
                raise ValueError("text must contain between 1 and 4000 characters")
            payload["text"] = text
        if action == "key":
            key = str(args.get("key") or "").strip()
            if not re.fullmatch(r"[A-Za-z0-9_+ -]{1,80}", key):
                raise ValueError("key contains unsupported characters")
            payload["key"] = "+".join(
                DESKTOP_KEY_ALIASES.get(part.lower(), part)
                for part in key.replace(" ", "+").split("+")
                if part
            )
        if action == "scroll":
            direction = str(args.get("direction") or "down").lower()
            if direction not in {"up", "down"}:
                raise ValueError("scroll direction must be up or down")
            try:
                amount = int(args.get("amount") or 1)
            except (TypeError, ValueError) as exc:
                raise ValueError("scroll amount must be an integer") from exc
            if not 1 <= amount <= 20:
                raise ValueError("scroll amount must be between 1 and 20")
            payload.update(direction=direction, amount=amount)
        await runtime.desktop_action(self._sandbox_id(session), action, payload)
        event_payload = {"action": action}
        if action in {"move", "click", "double_click", "drag"}:
            event_payload.update({key: payload[key] for key in payload if key != "button"})
        if action == "type":
            event_payload["characters"] = len(payload["text"])
        await self._record_event(session, "computer_input", event_payload)
        return ToolResult(
            data={"status": "success", "action": action, "session": _public_session(session)},
            summary=f"Cloud computer {action} input succeeded",
        )

    async def _cloud_export(self, runtime: SandboxRuntime, session: dict[str, Any], args: dict[str, Any]) -> ToolResult:
        sandbox_id = self._sandbox_id(session)
        path = remote_path(str(args.get("path") or "."))
        probe = await runtime.run(
            sandbox_id,
            f"if [ -d {shlex.quote(path)} ]; then echo dir; elif [ -f {shlex.quote(path)} ]; then echo file; else echo none; fi",
            cwd=SANDBOX_ROOT,
            timeout_seconds=15,
        )
        kind_fs = (probe.get("stdout") or "").strip()
        if kind_fs == "none":
            raise FileNotFoundError(self._rel(path))
        if kind_fs == "dir":
            archive = f"/tmp/export-{uuid.uuid4().hex}.zip"
            rel = self._rel(path)
            zip_result = await runtime.run(
                sandbox_id,
                f"rm -f {shlex.quote(archive)} && cd {shlex.quote(SANDBOX_ROOT)} && zip -rq {shlex.quote(archive)} {shlex.quote(rel)}",
                cwd=SANDBOX_ROOT,
                timeout_seconds=CLOUD_MAX_TIMEOUT_SECONDS,
            )
            if zip_result.get("status") != "success":
                raise RuntimeError(f"export archive failed: {zip_result.get('stderr') or zip_result.get('status')}")
            raw = await runtime.read(sandbox_id, archive)
            kind = "file"
            mime_type = "application/zip"
            title = str(args.get("title") or f"{posixpath.basename(path) or 'computer'} export.zip")
        else:
            raw = await runtime.read(sandbox_id, path)
            kind = str(args.get("kind") or "file")
            mime_type = str(args.get("mime_type") or "application/octet-stream")
            title = str(args.get("title") or posixpath.basename(path))
        artifact_id = await save_artifact(
            raw,
            kind=kind,
            title=title,
            task_id=_uuid_or_none(session.get("task_id")),
            org_id=session["organization_id"],
            region=session.get("region") or settings.region,
            mime_type=mime_type,
            created_by=session.get("member_id"),
        )
        await self._record_event(session, "computer_artifact_exported", {"path": self._rel(path), "artifact_id": artifact_id})
        return ToolResult(
            data={"session": _public_session(session), "artifact_id": artifact_id, "path": self._rel(path)},
            summary=f"Exported computer file to artifact {artifact_id}",
        )

    async def create_local_grant(
        self,
        *,
        organization_id: str,
        member_id: str,
        folder_path: str,
        purpose: str,
        task_id: str | None,
    ) -> dict[str, Any]:
        root = Path(folder_path).expanduser().resolve()
        if not root.is_dir():
            raise FileNotFoundError(folder_path)
        grant = {
            "id": str(uuid.uuid4()),
            "organization_id": organization_id,
            "region": settings.region,
            "member_id": member_id,
            "task_id": task_id,
            "folder_path": str(root),
            "purpose": purpose,
            "status": "active",
            "allowed_commands": [],
            "allowed_apps": [],
            "created_at": _now(),
            "updated_at": _now(),
            "revoked_at": None,
        }
        await self._save_grant(grant)
        await self._record_local_event(grant, "local_grant_created", {"folder_path": str(root), "purpose": purpose})
        return _public_grant(grant)

    async def revoke_local_grant(self, grant_id: str, *, organization_id: str) -> dict[str, Any]:
        grant = await self._load_grant(grant_id, organization_id)
        grant.update({"status": "revoked", "revoked_at": _now(), "updated_at": _now()})
        await self._save_grant(grant)
        await self._record_local_event(grant, "local_grant_revoked", {})
        return _public_grant(grant)

    async def _local_list(self, grant: dict[str, Any], root: Path, args: dict[str, Any]) -> ToolResult:
        path = self._grant_path(grant, root, str(args.get("path") or "."))
        children = [path] if path.is_file() else sorted(path.iterdir(), key=lambda item: item.name)[:200]
        entries = [
            {
                "path": str(child.relative_to(root)),
                "type": "directory" if child.is_dir() else "file",
                "size": None if child.is_dir() else child.stat().st_size,
            }
            for child in children
        ]
        await self._record_local_event(grant, "local_files_listed", {"path": str(path.relative_to(root)), "count": len(entries)})
        return ToolResult(data={"grant": _public_grant(grant), "entries": entries}, summary=f"Listed {len(entries)} local files")

    async def _local_read(self, grant: dict[str, Any], root: Path, args: dict[str, Any]) -> ToolResult:
        path = self._grant_path(grant, root, str(args.get("path") or ""))
        if not path.is_file():
            raise FileNotFoundError(str(args.get("path") or ""))
        raw = path.read_bytes()
        await self._record_local_event(grant, "local_file_read", {"path": str(path.relative_to(root)), "bytes": len(raw)})
        return ToolResult(
            data={
                "grant": _public_grant(grant),
                "path": str(path.relative_to(root)),
                "content": raw[:MAX_READ_BYTES].decode("utf-8", errors="replace"),
                "bytes": len(raw),
                "truncated": len(raw) > MAX_READ_BYTES,
            },
            summary=f"Read local file {path.relative_to(root)}",
        )

    async def _local_exec(self, grant: dict[str, Any], root: Path, args: dict[str, Any]) -> ToolResult:
        command = str(args.get("command") or "")
        result = await _run_shell(command, cwd=root, timeout_seconds=_timeout(args.get("timeout_seconds")))
        await self._record_local_event(
            grant,
            "local_command",
            {"command": command, "status": result["status"], "returncode": result["returncode"]},
        )
        return ToolResult(
            data={**result, "grant": _public_grant(grant), "workspace": str(root)},
            summary=f"Local computer command {result['status']}",
        )

    async def _local_open_app(self, grant: dict[str, Any], args: dict[str, Any]) -> ToolResult:
        app = str(args.get("app") or "").strip()
        if not app:
            raise ValueError("app is required")
        status = "degraded"
        reason = "Local app launching is recorded but disabled in the API runtime."
        if os.name == "posix" and shutil.which("open"):
            # Do not actually launch apps from backend tests/servers; the desktop bridge
            # can swap this metadata-only implementation for an out-of-process bridge.
            status = "degraded"
        await self._record_local_event(grant, "local_app_open_requested", {"app": app, "status": status})
        return ToolResult(
            data={"status": status, "reason": reason, "grant": _public_grant(grant), "app": app},
            summary=f"Local app open request recorded for {app}",
        )

    def _grant_path(self, grant: dict[str, Any], root: Path, requested: str) -> Path:
        if grant.get("status") != "active":
            raise PermissionError("local computer grant is not active")
        try:
            path = jailed_path(root, requested)
        except ValueError as exc:
            raise PermissionError("Path escapes the authorized local folder") from exc
        if root != path and root not in path.parents:
            raise PermissionError("Path escapes the authorized local folder")
        return path

    async def _load_session(self, session_id: str, organization_id: str) -> dict[str, Any]:
        try:
            table = await reflect_table("computer_sessions")
            async with engine.begin() as conn:
                row = (
                    await conn.execute(
                        select(table).where(table.c.id == session_id, table.c.organization_id == organization_id)
                    )
                ).mappings().first()
            if not row:
                raise KeyError(session_id)
            return _coerce_session(dict(row))
        except KeyError:
            session = self._sessions.get(session_id)
            if session and session["organization_id"] == organization_id:
                return session
            raise
        except Exception as exc:
            if settings.is_production:
                raise RuntimeUnavailable("computer session storage is unavailable") from exc
            session = self._sessions.get(session_id)
            if not session or session["organization_id"] != organization_id:
                raise KeyError(session_id)
            return session

    async def _save_session(self, session: dict[str, Any]) -> None:
        session["updated_at"] = session.get("updated_at") or _now()
        self._sessions[session["id"]] = session
        try:
            table = await reflect_table("computer_sessions")
            values = {key: session.get(key) for key in table.c.keys() if key in session and key not in {"created_at", "updated_at"}}
            async with engine.begin() as conn:
                existing = (await conn.execute(select(table.c.id).where(table.c.id == session["id"]))).first()
                if existing:
                    await conn.execute(update(table).where(table.c.id == session["id"]).values(**values, updated_at=_now()))
                else:
                    await conn.execute(insert(table).values(**values))
        except Exception as exc:
            if settings.is_production:
                raise RuntimeUnavailable("computer session storage is unavailable") from exc

    async def _record_event(self, session: dict[str, Any], event_type: str, payload: dict[str, Any]) -> None:
        event = {
            "id": str(uuid.uuid4()),
            "organization_id": session["organization_id"],
            "session_id": session["id"],
            "task_id": session.get("task_id"),
            "seq": len(self._events.get(session["id"], [])) + 1,
            "event_type": event_type,
            "payload": payload,
            "created_at": _now(),
        }
        self._events.setdefault(session["id"], []).append(event)
        session["history"] = (list(session.get("history") or []) + [{"event_type": event_type, "payload": payload, "created_at": _stamp()}])[-100:]
        await self._save_session(session)
        try:
            table = await reflect_table("computer_session_events")
            async with engine.begin() as conn:
                seq = (
                    await conn.execute(
                        select(func.coalesce(func.max(table.c.seq), 0)).where(
                            table.c.organization_id == session["organization_id"],
                            table.c.session_id == session["id"],
                        )
                    )
                ).scalar_one()
                await conn.execute(
                    insert(table).values(
                        organization_id=session["organization_id"],
                        region=session.get("region") or settings.region,
                        session_id=session["id"],
                        task_id=session.get("task_id"),
                        seq=int(seq) + 1,
                        event_type=event_type,
                        payload=payload,
                    )
                )
        except Exception as exc:
            if settings.is_production:
                raise RuntimeUnavailable("computer event storage is unavailable") from exc
        try:
            await audit.log(
                "activity",
                "chronos",
                event_type,
                organization_id=session["organization_id"],
                resource_type="tasks" if session.get("task_id") else "computer_sessions",
                resource_id=session.get("task_id") or session["id"],
                payload={"type": event_type, "session_id": session["id"], "task_id": session.get("task_id"), **payload},
            )
        except Exception:
            pass

    async def list_events(self, session_id: str, *, organization_id: str) -> list[dict[str, Any]]:
        try:
            table = await reflect_table("computer_session_events")
            async with engine.begin() as conn:
                rows = (
                    await conn.execute(
                        select(table).where(table.c.organization_id == organization_id, table.c.session_id == session_id).order_by(table.c.seq)
                    )
                ).mappings().all()
            return [dict(row) for row in rows]
        except Exception as exc:
            if settings.is_production:
                raise RuntimeUnavailable("computer event storage is unavailable") from exc
            return list(self._events.get(session_id, []))

    async def list_sessions(
        self,
        *,
        organization_id: str,
        member_id: str | None = None,
        task_id: str | None = None,
    ) -> list[dict[str, Any]]:
        try:
            table = await reflect_table("computer_sessions")
            async with engine.begin() as conn:
                rows = (
                    await conn.execute(
                        select(table)
                        .where(
                            table.c.organization_id == organization_id,
                            *([table.c.member_id == member_id] if member_id else []),
                            *([table.c.task_id == task_id] if task_id else []),
                        )
                        .order_by(table.c.updated_at.desc())
                    )
                ).mappings().all()
            return [_public_session(_coerce_session(dict(row))) for row in rows]
        except Exception as exc:
            if settings.is_production:
                raise RuntimeUnavailable("computer session storage is unavailable") from exc
            sessions = [
                session
                for session in self._sessions.values()
                if session["organization_id"] == organization_id
                and (member_id is None or str(session.get("member_id")) == member_id)
                and (task_id is None or str(session.get("task_id")) == task_id)
            ]
            return [_public_session(session) for session in sorted(sessions, key=lambda item: str(item.get("updated_at") or ""), reverse=True)]

    async def cancel_task_sessions(
        self,
        *,
        organization_id: str,
        task_ids: list[str],
        member_id: str,
    ) -> dict[str, Any]:
        """Destroy only E2B sessions durably bound to this tenant/task set."""

        scoped_ids = {str(task_id) for task_id in task_ids if task_id}
        if not scoped_ids:
            return {"status": "complete", "cancelled": 0}
        try:
            table = await reflect_table("computer_sessions")
            async with engine.begin() as conn:
                rows = (
                    await conn.execute(
                        select(table).where(
                            table.c.organization_id == organization_id,
                            table.c.task_id.in_(sorted(scoped_ids)),
                        )
                    )
                ).mappings().all()
            sessions = [_coerce_session(dict(row)) for row in rows]
        except Exception as exc:
            if settings.is_production:
                raise RuntimeUnavailable("computer cancellation storage scan is unavailable") from exc
            sessions = [
                session
                for session in self._sessions.values()
                if session.get("organization_id") == organization_id
                and str(session.get("task_id") or "") in scoped_ids
            ]

        active = [
            session
            for session in sessions
            if session.get("status") not in {"cancelled", "expired"}
        ]
        if not active:
            return {"status": "complete", "cancelled": 0}
        runtime = self._get_runtime()
        if runtime is None:
            raise RuntimeUnavailable("computer cancellation requires the E2B runtime")
        cancelled = 0
        failures: list[str] = []
        for session in active:
            try:
                await self._cancel_session(runtime, session, member_id=member_id)
                cancelled += 1
            except Exception:
                failures.append(str(session["id"]))
        if failures:
            raise RuntimeUnavailable(
                f"computer provider cleanup failed for {len(failures)} session(s)"
            )
        return {"status": "complete", "cancelled": cancelled}

    async def cleanup_expired_sessions(self, *, limit: int = 100) -> int:
        """Destroy provider sandboxes after the consent hard-expiry.

        E2B auto-pauses idle sessions and preserves their state. This leader-only
        cleanup is the hard deletion boundary that prevents paused customer data
        from being retained beyond the user-authorized window.
        """

        try:
            table = await reflect_table("computer_sessions")
            async with engine.begin() as conn:
                rows = (
                    await conn.execute(
                        select(table)
                        .where(table.c.status.in_(["active", "paused", "provisioning"]))
                        .order_by(table.c.created_at)
                        .limit(max(1, min(limit, 500)))
                    )
                ).mappings().all()
            candidates = [_coerce_session(dict(row)) for row in rows]
        except Exception as exc:
            if settings.is_production:
                raise RuntimeUnavailable("computer expiry storage scan is unavailable") from exc
            candidates = [
                session
                for session in self._sessions.values()
                if session.get("status") in ACTIVE_SESSION_STATUSES
            ][:limit]
        runtime = self._get_runtime()
        if runtime is None:
            if settings.is_production:
                raise RuntimeUnavailable("computer expiry cleanup requires the E2B runtime")
            return 0
        cleaned = 0
        for session in candidates:
            try:
                expires_at = _parse_timestamp(_session_consent(session).get("expires_at"))
            except (TypeError, ValueError):
                expires_at = _now()
            if expires_at > _now():
                continue
            try:
                await runtime.resume(
                    self._sandbox_id(session),
                    timeout_seconds=60,
                    expected_metadata=self._provider_metadata(session),
                )
                await runtime.kill(self._sandbox_id(session))
            except SandboxExpired:
                pass
            session.update({"status": "expired", "closed_at": _now(), "updated_at": _now()})
            await self._save_session(session)
            await self._record_event(session, "computer_session_expired", {"source": "scheduler"})
            cleaned += 1
        return cleaned

    async def _load_grant(self, grant_id: str, organization_id: str) -> dict[str, Any]:
        if not grant_id:
            raise PermissionError("grant_id is required")
        try:
            table = await reflect_table("local_computer_grants")
            async with engine.begin() as conn:
                row = (
                    await conn.execute(
                        select(table).where(table.c.id == grant_id, table.c.organization_id == organization_id)
                    )
                ).mappings().first()
            if not row:
                raise PermissionError("local computer grant not found")
            grant = _coerce_grant(dict(row))
        except PermissionError:
            raise
        except Exception:
            grant = self._grants.get(grant_id)
            if not grant or grant["organization_id"] != organization_id:
                raise PermissionError("local computer grant not found")
        if grant.get("status") != "active":
            raise PermissionError("local computer grant is not active")
        return grant

    async def _save_grant(self, grant: dict[str, Any]) -> None:
        grant["updated_at"] = grant.get("updated_at") or _now()
        self._grants[grant["id"]] = grant
        try:
            table = await reflect_table("local_computer_grants")
            values = {key: grant.get(key) for key in table.c.keys() if key in grant and key not in {"created_at", "updated_at"}}
            async with engine.begin() as conn:
                existing = (await conn.execute(select(table.c.id).where(table.c.id == grant["id"]))).first()
                if existing:
                    await conn.execute(update(table).where(table.c.id == grant["id"]).values(**values, updated_at=_now()))
                else:
                    await conn.execute(insert(table).values(**values))
        except Exception:
            return

    async def _record_local_event(self, grant: dict[str, Any], event_type: str, payload: dict[str, Any]) -> None:
        event = {
            "id": str(uuid.uuid4()),
            "organization_id": grant["organization_id"],
            "grant_id": grant["id"],
            "task_id": grant.get("task_id"),
            "seq": len(self._local_events.get(grant["id"], [])) + 1,
            "event_type": event_type,
            "payload": payload,
            "created_at": _now(),
        }
        self._local_events.setdefault(grant["id"], []).append(event)
        try:
            table = await reflect_table("local_computer_events")
            async with engine.begin() as conn:
                seq = (
                    await conn.execute(
                        select(func.coalesce(func.max(table.c.seq), 0)).where(
                            table.c.organization_id == grant["organization_id"],
                            table.c.grant_id == grant["id"],
                        )
                    )
                ).scalar_one()
                await conn.execute(
                    insert(table).values(
                        organization_id=grant["organization_id"],
                        region=grant.get("region") or settings.region,
                        grant_id=grant["id"],
                        task_id=grant.get("task_id"),
                        seq=int(seq) + 1,
                        event_type=event_type,
                        payload=payload,
                    )
                )
        except Exception:
            pass
        try:
            await audit.log(
                "activity",
                "chronos",
                event_type,
                organization_id=grant["organization_id"],
                resource_type="local_computer_grants",
                resource_id=grant["id"],
                payload={"type": event_type, "grant_id": grant["id"], "task_id": grant.get("task_id"), **payload},
            )
        except Exception:
            pass

    async def list_local_events(self, grant_id: str, *, organization_id: str) -> list[dict[str, Any]]:
        try:
            table = await reflect_table("local_computer_events")
            async with engine.begin() as conn:
                rows = (
                    await conn.execute(
                        select(table).where(table.c.organization_id == organization_id, table.c.grant_id == grant_id).order_by(table.c.seq)
                    )
                ).mappings().all()
            return [dict(row) for row in rows]
        except Exception:
            return list(self._local_events.get(grant_id, []))

    async def list_local_grants(
        self, *, organization_id: str, member_id: str | None = None
    ) -> list[dict[str, Any]]:
        try:
            table = await reflect_table("local_computer_grants")
            async with engine.begin() as conn:
                rows = (
                    await conn.execute(
                        select(table)
                        .where(
                            table.c.organization_id == organization_id,
                            *([table.c.member_id == member_id] if member_id else []),
                        )
                        .order_by(table.c.updated_at.desc())
                    )
                ).mappings().all()
            return [_public_grant(_coerce_grant(dict(row))) for row in rows]
        except Exception:
            grants = [
                grant
                for grant in self._grants.values()
                if grant["organization_id"] == organization_id
                and (member_id is None or str(grant.get("member_id")) == member_id)
            ]
            return [_public_grant(grant) for grant in sorted(grants, key=lambda item: str(item.get("updated_at") or ""), reverse=True)]


computer_connector = ComputerConnector()
