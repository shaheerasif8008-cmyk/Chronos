from __future__ import annotations

import asyncio
import os
import resource
import shutil
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import func, insert, select, update

from core import audit
from core.artifacts import save_artifact
from core.config import settings
from core.db import engine, reflect_table
from core.models import ToolResult
from core.workspace import jailed_path

CLOUD_ROOT = Path("/tmp/chronos_computers")
MAX_READ_BYTES = 512_000
MAX_WRITE_BYTES = 512_000
MAX_OUTPUT_BYTES = 128_000
MAX_TIMEOUT_SECONDS = 30
DEFAULT_TIMEOUT_SECONDS = 10
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


def _cloud_workspace(org_id: str, session_id: str) -> Path:
    root = (CLOUD_ROOT / str(org_id or "default") / session_id).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _public_session(session: dict[str, Any]) -> dict[str, Any]:
    safe = dict(session)
    safe.pop("environment", None)
    return safe


def _public_grant(grant: dict[str, Any]) -> dict[str, Any]:
    return dict(grant)


def _coerce_session(row: dict[str, Any]) -> dict[str, Any]:
    if row.get("id") is not None:
        row["id"] = str(row["id"])
    row.setdefault("environment", {})
    row.setdefault("resource_limits", {})
    row.setdefault("network_policy", {})
    row.setdefault("history", [])
    return row


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


async def _run_shell(command: str, *, cwd: Path, timeout_seconds: int) -> dict[str, Any]:
    _validate_command(command)
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(cwd),
        "TMPDIR": str(cwd / ".tmp"),
        "PYTHONNOUSERSITE": "1",
    }
    (cwd / ".tmp").mkdir(exist_ok=True)
    process = await asyncio.create_subprocess_exec(
        "/bin/sh",
        "-lc",
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

    async def execute(self, tool: str, args: dict[str, Any]) -> ToolResult:
        args.pop("__connector_tier", None)
        org_id = str(args.pop("__org_id", settings.org_id) or settings.org_id)
        task_id = str(args.pop("__task_id", "") or "")
        provider, action = tool.split(".", 1)
        if provider == "computer":
            return await self._execute_cloud(action, args, organization_id=org_id, task_id=task_id or None)
        if provider == "local_computer":
            return await self._execute_local(action, args, organization_id=org_id, task_id=task_id or None)
        raise ValueError(f"Unknown computer tool: {tool}")

    async def _execute_cloud(
        self, action: str, args: dict[str, Any], *, organization_id: str, task_id: str | None
    ) -> ToolResult:
        if action == "create_session":
            session = await self.create_session(
                organization_id=organization_id,
                member_id=str(args.get("member_id") or "chronos"),
                task_id=task_id,
                purpose=str(args.get("purpose") or "computer task"),
            )
            return ToolResult(data={"session": session}, summary="Cloud computer session created")

        session = await self._load_or_create_session(args, organization_id=organization_id, task_id=task_id)
        workspace = _cloud_workspace(organization_id, session["id"])
        if action == "list_files":
            return await self._cloud_list(session, workspace, args)
        if action == "read_file":
            return await self._cloud_read(session, workspace, args)
        if action == "write_file":
            return await self._cloud_write(session, workspace, args)
        if action == "exec":
            return await self._cloud_exec(session, workspace, args)
        if action == "install_package":
            command = self._package_install_command(args)
            return await self._cloud_exec(session, workspace, {"command": command, "timeout_seconds": args.get("timeout_seconds", 30)}, event_type="computer_package_install")
        if action == "screenshot":
            return await self._cloud_screenshot(session)
        if action == "export_artifact":
            return await self._cloud_export(session, workspace, args)
        raise ValueError(f"Unknown computer tool: computer.{action}")

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

    async def create_session(
        self, *, organization_id: str, member_id: str, task_id: str | None, purpose: str
    ) -> dict[str, Any]:
        session_id = str(uuid.uuid4())
        workspace = _cloud_workspace(organization_id, session_id)
        session = {
            "id": session_id,
            "organization_id": organization_id,
            "region": settings.region,
            "task_id": task_id,
            "member_id": member_id,
            "status": "active",
            "purpose": purpose,
            "workspace_path": str(workspace),
            "browser_session_id": None,
            "editor_state": {},
            "network_policy": {"mode": "restricted", "allowed": ["package_index"]},
            "resource_limits": {"timeout_seconds": MAX_TIMEOUT_SECONDS, "output_bytes": MAX_OUTPUT_BYTES},
            "environment": {},
            "history": [],
            "created_at": _now(),
            "updated_at": _now(),
            "closed_at": None,
        }
        await self._save_session(session)
        await self._record_event(session, "computer_session_created", {"purpose": purpose})
        return _public_session(session)

    async def _load_or_create_session(
        self, args: dict[str, Any], *, organization_id: str, task_id: str | None
    ) -> dict[str, Any]:
        session_id = str(args.get("session_id") or "")
        if session_id:
            return await self._load_session(session_id, organization_id)
        return await self.create_session(
            organization_id=organization_id,
            member_id=str(args.get("member_id") or "chronos"),
            task_id=task_id,
            purpose=str(args.get("purpose") or "computer task"),
        )

    async def _cloud_list(self, session: dict[str, Any], root: Path, args: dict[str, Any]) -> ToolResult:
        path = jailed_path(root, str(args.get("path") or "."))
        if not path.exists():
            raise FileNotFoundError(str(args.get("path") or "."))
        children = [path] if path.is_file() else sorted(path.iterdir(), key=lambda item: item.name)[:200]
        entries = [
            {
                "path": str(child.relative_to(root)),
                "type": "directory" if child.is_dir() else "file",
                "size": None if child.is_dir() else child.stat().st_size,
            }
            for child in children
        ]
        await self._record_event(session, "computer_files_listed", {"path": str(path.relative_to(root)), "count": len(entries)})
        return ToolResult(data={"session": _public_session(session), "entries": entries}, summary=f"Listed {len(entries)} computer files")

    async def _cloud_read(self, session: dict[str, Any], root: Path, args: dict[str, Any]) -> ToolResult:
        path = jailed_path(root, str(args.get("path") or ""))
        if not path.is_file():
            raise FileNotFoundError(str(args.get("path") or ""))
        raw = path.read_bytes()
        await self._record_event(session, "computer_file_read", {"path": str(path.relative_to(root)), "bytes": len(raw)})
        return ToolResult(
            data={
                "session": _public_session(session),
                "path": str(path.relative_to(root)),
                "content": raw[:MAX_READ_BYTES].decode("utf-8", errors="replace"),
                "bytes": len(raw),
                "truncated": len(raw) > MAX_READ_BYTES,
            },
            summary=f"Read computer file {path.relative_to(root)}",
        )

    async def _cloud_write(self, session: dict[str, Any], root: Path, args: dict[str, Any]) -> ToolResult:
        path = jailed_path(root, str(args.get("path") or ""))
        raw = str(args.get("content") or "").encode("utf-8")
        if len(raw) > MAX_WRITE_BYTES:
            raise ValueError(f"computer.write_file payload exceeds {MAX_WRITE_BYTES} bytes")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        await self._record_event(session, "computer_file_written", {"path": str(path.relative_to(root)), "bytes": len(raw)})
        return ToolResult(
            data={"session": _public_session(session), "path": str(path.relative_to(root)), "bytes": len(raw)},
            summary=f"Wrote computer file {path.relative_to(root)}",
        )

    async def _cloud_exec(
        self,
        session: dict[str, Any],
        root: Path,
        args: dict[str, Any],
        *,
        event_type: str = "computer_command",
    ) -> ToolResult:
        command = str(args.get("command") or "")
        result = await _run_shell(command, cwd=root, timeout_seconds=_timeout(args.get("timeout_seconds")))
        payload = {
            "command": command,
            "status": result["status"],
            "returncode": result["returncode"],
            "stdout_bytes": len(result["stdout"].encode("utf-8")),
            "stderr_bytes": len(result["stderr"].encode("utf-8")),
        }
        await self._record_event(session, event_type, payload)
        return ToolResult(
            data={**result, "session": _public_session(session), "workspace": str(root)},
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

    async def _cloud_screenshot(self, session: dict[str, Any]) -> ToolResult:
        await self._record_event(session, "computer_screenshot", {"status": "degraded"})
        return ToolResult(
            data={
                "status": "degraded",
                "reason": "Cloud desktop screenshot capture is unavailable in this runtime; session metadata is durable.",
                "session": _public_session(session),
            },
            summary="Computer screenshot unavailable in metadata-only runtime",
        )

    async def _cloud_export(self, session: dict[str, Any], root: Path, args: dict[str, Any]) -> ToolResult:
        path = jailed_path(root, str(args.get("path") or "."))
        if not path.exists():
            raise FileNotFoundError(str(args.get("path") or "."))
        if path.is_dir():
            archive_base = root / f"export-{uuid.uuid4()}"
            archive_path = shutil.make_archive(str(archive_base), "zip", root_dir=path)
            raw = Path(archive_path).read_bytes()
            kind = "file"
            mime_type = "application/zip"
            title = str(args.get("title") or f"{path.name or 'computer'} export.zip")
        else:
            raw = path.read_bytes()
            kind = str(args.get("kind") or "file")
            mime_type = str(args.get("mime_type") or "application/octet-stream")
            title = str(args.get("title") or path.name)
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
        await self._record_event(session, "computer_artifact_exported", {"path": str(path.relative_to(root)), "artifact_id": artifact_id})
        return ToolResult(
            data={"session": _public_session(session), "artifact_id": artifact_id, "path": str(path.relative_to(root))},
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
        except Exception:
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
        except Exception:
            return

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
        except Exception:
            pass
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
        except Exception:
            return list(self._events.get(session_id, []))

    async def list_sessions(self, *, organization_id: str) -> list[dict[str, Any]]:
        try:
            table = await reflect_table("computer_sessions")
            async with engine.begin() as conn:
                rows = (
                    await conn.execute(
                        select(table).where(table.c.organization_id == organization_id).order_by(table.c.updated_at.desc())
                    )
                ).mappings().all()
            return [_public_session(_coerce_session(dict(row))) for row in rows]
        except Exception:
            sessions = [session for session in self._sessions.values() if session["organization_id"] == organization_id]
            return [_public_session(session) for session in sorted(sessions, key=lambda item: str(item.get("updated_at") or ""), reverse=True)]

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

    async def list_local_grants(self, *, organization_id: str) -> list[dict[str, Any]]:
        try:
            table = await reflect_table("local_computer_grants")
            async with engine.begin() as conn:
                rows = (
                    await conn.execute(
                        select(table).where(table.c.organization_id == organization_id).order_by(table.c.updated_at.desc())
                    )
                ).mappings().all()
            return [_public_grant(_coerce_grant(dict(row))) for row in rows]
        except Exception:
            grants = [grant for grant in self._grants.values() if grant["organization_id"] == organization_id]
            return [_public_grant(grant) for grant in sorted(grants, key=lambda item: str(item.get("updated_at") or ""), reverse=True)]


computer_connector = ComputerConnector()
