from __future__ import annotations

"""Desktop GUI operator — real virtual-display computer-use bridge.

Each desktop session owns a virtual X display (Xvfb) into which GUI apps are
launched. The agent perceives the desktop through `screenshot` (PNG → data URL,
fed back to the vision loop) and acts through `move`, `click`, `type`, `key`,
and `scroll`, all driven by xdotool. This gives genuine, governed desktop
operation that runs inside the API container — no external VM required.

Like the browser operator, the bridge degrades truthfully: if Xvfb / xdotool /
scrot are not present, or a display cannot be launched, sessions report
`status="degraded"` and actions return a degraded result instead of crashing.

Every action routes through the ToolBroker. Session metadata persists to
Postgres when the migration is present, with an in-process fallback for tests
and partially-migrated dev environments.
"""

import asyncio
import base64
import os
import shutil
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import func, insert, select, update

from core import audit
from core.config import settings
from core.db import engine, reflect_table
from core.models import ToolResult

DESKTOP_ROOT = Path("/tmp/chronos_desktops")
SCREEN_W = 1280
SCREEN_H = 800
SCREEN_DEPTH = 24
_DISPLAY_BASE = 100
_TYPE_DELAY_MS = 12
_LAUNCH_TIMEOUT_S = 8.0
# open_app commands are validated against these before launch.
_FORBIDDEN_COMMAND_MARKERS = (
    " rm -rf ",
    "mkfs",
    "diskutil erase",
    "shutdown",
    "reboot",
    ":(){",
)
_VALID_BUTTONS = {"left": 1, "middle": 2, "right": 3}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp() -> str:
    return _now().isoformat()


def _public_session(session: dict[str, Any]) -> dict[str, Any]:
    safe = dict(session)
    safe.pop("_runtime", None)
    return safe


def _coerce_session(row: dict[str, Any]) -> dict[str, Any]:
    if row.get("id") is not None:
        row["id"] = str(row["id"])
    row.setdefault("consent", {})
    row.setdefault("history", [])
    return row


def _tools_available() -> tuple[bool, str | None]:
    for tool in ("Xvfb", "xdotool", "scrot"):
        if not shutil.which(tool):
            return False, f"{tool} is not installed in this runtime"
    return True, None


class DesktopConnector:
    def __init__(self) -> None:
        self._sessions: dict[str, dict[str, Any]] = {}
        # session_id → {"display": ":101", "proc": Popen, "apps": [Popen, ...]}
        self._runtimes: dict[str, dict[str, Any]] = {}

    # ── Broker entrypoint ──────────────────────────────────────────────────
    async def execute(self, tool: str, args: dict[str, Any]) -> ToolResult:
        args.pop("__connector_tier", None)
        org_id = str(args.pop("__org_id", settings.org_id) or settings.org_id)
        task_id = str(args.pop("__task_id", "") or "") or None
        action = tool.split(".", 1)[1]

        if action == "create_session":
            session = await self.create_session(
                organization_id=org_id,
                member_id=str(args.get("member_id") or "chronos"),
                task_id=task_id,
                purpose=str(args.get("purpose") or "desktop task"),
                consent=args.get("consent") if isinstance(args.get("consent"), dict) else {},
            )
            return ToolResult(data={"session": session}, summary="Desktop session created")

        session = await self._load_or_create(args, organization_id=org_id, task_id=task_id)
        if session.get("status") == "revoked":
            raise ValueError("desktop session has been revoked")

        if action == "screenshot":
            return await self._screenshot(session)
        if action == "move":
            return await self._move(session, args)
        if action == "click":
            return await self._click(session, args)
        if action == "type":
            return await self._type(session, args)
        if action == "key":
            return await self._key(session, args)
        if action == "scroll":
            return await self._scroll(session, args)
        if action == "open_app":
            return await self._open_app(session, args)
        if action == "get_state":
            return ToolResult(data={"session": _public_session(session)}, summary="Desktop session state")
        if action == "close":
            closed = await self.close_session(session["id"], organization_id=session["organization_id"])
            return ToolResult(data={"session": closed}, summary="Desktop session closed")
        raise ValueError(f"Unknown desktop tool: {tool}")

    # ── Session lifecycle ──────────────────────────────────────────────────
    async def create_session(
        self,
        *,
        organization_id: str,
        member_id: str,
        task_id: str | None,
        purpose: str,
        consent: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        session_id = str(uuid.uuid4())
        available, reason = _tools_available()
        session = {
            "id": session_id,
            "organization_id": organization_id,
            "region": settings.region,
            "task_id": task_id,
            "member_id": member_id,
            "status": "active" if available else "degraded",
            "purpose": purpose,
            "consent": consent or {},
            "display": None,
            "screen": f"{SCREEN_W}x{SCREEN_H}",
            "degraded_reason": None if available else reason,
            "screenshot_object_path": None,
            "history": [],
            "created_at": _now(),
            "updated_at": _now(),
            "closed_at": None,
        }
        await self._save_session(session)
        await self._record_event(session, "desktop_session_created", {"purpose": purpose, "status": session["status"]})
        return _public_session(session)

    async def close_session(self, session_id: str, *, organization_id: str) -> dict[str, Any]:
        session = await self._load_session(session_id, organization_id)
        session.update({"status": "closed", "closed_at": _now(), "updated_at": _now()})
        await self._save_session(session)
        await self._teardown_runtime(session_id)
        await self._record_event(session, "desktop_session_closed", {})
        return _public_session(session)

    async def revoke_session(self, session_id: str, *, organization_id: str, reason: str) -> dict[str, Any]:
        session = await self._load_session(session_id, organization_id)
        session.update({"status": "revoked", "updated_at": _now()})
        await self._save_session(session)
        await self._teardown_runtime(session_id)
        await self._record_event(session, "desktop_session_revoked", {"reason": reason})
        return _public_session(session)

    async def list_sessions(self, *, organization_id: str, task_id: str | None = None) -> list[dict[str, Any]]:
        try:
            table = await reflect_table("desktop_sessions")
            stmt = select(table).where(table.c.organization_id == organization_id)
            if task_id:
                stmt = stmt.where(table.c.task_id == task_id)
            stmt = stmt.order_by(table.c.updated_at.desc())
            async with engine.begin() as conn:
                rows = (await conn.execute(stmt)).mappings().all()
            return [_public_session(_coerce_session(dict(row))) for row in rows]
        except Exception:
            sessions = [
                _public_session(s)
                for s in self._sessions.values()
                if s["organization_id"] == organization_id and (task_id is None or s.get("task_id") == task_id)
            ]
            return sorted(sessions, key=lambda item: str(item.get("updated_at") or ""), reverse=True)

    async def list_events(self, session_id: str, *, organization_id: str) -> list[dict[str, Any]]:
        try:
            table = await reflect_table("desktop_session_events")
            async with engine.begin() as conn:
                rows = (
                    await conn.execute(
                        select(table)
                        .where(table.c.organization_id == organization_id, table.c.session_id == session_id)
                        .order_by(table.c.seq)
                    )
                ).mappings().all()
            return [dict(row) for row in rows]
        except Exception:
            session = self._sessions.get(session_id)
            return list(session.get("history") or []) if session else []

    # ── Actions ────────────────────────────────────────────────────────────
    async def _screenshot(self, session: dict[str, Any]) -> ToolResult:
        runtime = await self._ensure_runtime(session)
        if runtime is None:
            return await self._degraded(session, "screenshot", "no virtual display available")
        out_path = self._workspace(session) / f"shot-{uuid.uuid4().hex}.png"
        code, _, err = await self._run(["scrot", "-o", str(out_path)], runtime["display"])
        if code != 0 or not out_path.exists():
            return await self._degraded(session, "screenshot", f"scrot failed: {err[:160]}")
        raw = out_path.read_bytes()
        data_url = "data:image/png;base64," + base64.b64encode(raw).decode("ascii")
        object_path = f"desktop-screenshots/{session['id']}-{uuid.uuid4().hex}.png"
        session["screenshot_object_path"] = object_path
        await self._record_action(session, "screenshot", {"bytes": len(raw), "screenshot_object_path": object_path})
        return ToolResult(
            data={
                "session": _public_session(session),
                "screenshot_data_url": data_url,
                "width": SCREEN_W,
                "height": SCREEN_H,
            },
            summary=f"Captured desktop screenshot ({SCREEN_W}x{SCREEN_H})",
        )

    async def _move(self, session: dict[str, Any], args: dict[str, Any]) -> ToolResult:
        runtime = await self._ensure_runtime(session)
        if runtime is None:
            return await self._degraded(session, "move", "no virtual display available")
        x, y = self._coords(args)
        await self._run(["xdotool", "mousemove", str(x), str(y)], runtime["display"])
        await self._record_action(session, "move", {"x": x, "y": y})
        return ToolResult(data={"session": _public_session(session)}, summary=f"Moved pointer to ({x}, {y})")

    async def _click(self, session: dict[str, Any], args: dict[str, Any]) -> ToolResult:
        runtime = await self._ensure_runtime(session)
        if runtime is None:
            return await self._degraded(session, "click", "no virtual display available")
        button = _VALID_BUTTONS.get(str(args.get("button") or "left").lower(), 1)
        clicks = max(1, min(int(args.get("clicks") or 1), 3))
        cmd = ["xdotool"]
        if args.get("x") is not None and args.get("y") is not None:
            x, y = self._coords(args)
            cmd += ["mousemove", str(x), str(y)]
        else:
            x = y = None
        cmd += ["click", "--repeat", str(clicks), str(button)]
        await self._run(cmd, runtime["display"])
        await self._record_action(session, "click", {"x": x, "y": y, "button": button, "clicks": clicks})
        return ToolResult(data={"session": _public_session(session)}, summary=f"Clicked button {button} x{clicks}")

    async def _type(self, session: dict[str, Any], args: dict[str, Any]) -> ToolResult:
        runtime = await self._ensure_runtime(session)
        if runtime is None:
            return await self._degraded(session, "type", "no virtual display available")
        text = str(args.get("text") or "")
        if not text:
            raise ValueError("desktop.type requires 'text'")
        await self._run(["xdotool", "type", "--delay", str(_TYPE_DELAY_MS), "--", text], runtime["display"])
        await self._record_action(session, "type", {"text_length": len(text)})
        return ToolResult(data={"session": _public_session(session)}, summary=f"Typed {len(text)} characters")

    async def _key(self, session: dict[str, Any], args: dict[str, Any]) -> ToolResult:
        runtime = await self._ensure_runtime(session)
        if runtime is None:
            return await self._degraded(session, "key", "no virtual display available")
        keys = str(args.get("keys") or "").strip()
        if not keys:
            raise ValueError("desktop.key requires 'keys' (e.g. 'ctrl+s', 'Return')")
        await self._run(["xdotool", "key", "--", keys], runtime["display"])
        await self._record_action(session, "key", {"keys": keys})
        return ToolResult(data={"session": _public_session(session)}, summary=f"Pressed keys {keys}")

    async def _scroll(self, session: dict[str, Any], args: dict[str, Any]) -> ToolResult:
        runtime = await self._ensure_runtime(session)
        if runtime is None:
            return await self._degraded(session, "scroll", "no virtual display available")
        direction = str(args.get("direction") or "down").lower()
        amount = max(1, min(int(args.get("amount") or 3), 20))
        button = 4 if direction == "up" else 5
        await self._run(["xdotool", "click", "--repeat", str(amount), str(button)], runtime["display"])
        await self._record_action(session, "scroll", {"direction": direction, "amount": amount})
        return ToolResult(data={"session": _public_session(session)}, summary=f"Scrolled {direction} x{amount}")

    async def _open_app(self, session: dict[str, Any], args: dict[str, Any]) -> ToolResult:
        command = str(args.get("command") or "").strip()
        if not command:
            raise ValueError("desktop.open_app requires 'command'")
        normalized = f" {command.lower()} "
        if any(marker in normalized for marker in _FORBIDDEN_COMMAND_MARKERS):
            raise ValueError("desktop.open_app command rejected by safety policy")
        runtime = await self._ensure_runtime(session)
        if runtime is None:
            return await self._degraded(session, "open_app", "no virtual display available")
        env = {**os.environ, "DISPLAY": runtime["display"]}
        try:
            proc = subprocess.Popen(  # noqa: S602 — launched into the isolated virtual display
                ["/bin/sh", "-lc", command],
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception as exc:
            return await self._degraded(session, "open_app", f"launch failed: {exc}")
        runtime.setdefault("apps", []).append(proc)
        await asyncio.sleep(0.6)  # let the window map before the next screenshot
        await self._record_action(session, "open_app", {"command": command, "pid": proc.pid})
        return ToolResult(
            data={"session": _public_session(session), "command": command, "pid": proc.pid},
            summary=f"Launched app: {command}",
        )

    def _coords(self, args: dict[str, Any]) -> tuple[int, int]:
        x = max(0, min(int(args.get("x") or 0), SCREEN_W - 1))
        y = max(0, min(int(args.get("y") or 0), SCREEN_H - 1))
        return x, y

    async def _degraded(self, session: dict[str, Any], action: str, reason: str) -> ToolResult:
        session["status"] = "degraded"
        session["degraded_reason"] = reason
        await self._record_action(session, action, {"status": "degraded", "reason": reason})
        return ToolResult(
            data={"session": _public_session(session), "status": "degraded", "reason": reason},
            summary=f"Desktop {action} unavailable: {reason}",
        )

    # ── Virtual display runtime ────────────────────────────────────────────
    def _workspace(self, session: dict[str, Any]) -> Path:
        root = (DESKTOP_ROOT / str(session["organization_id"]) / session["id"]).resolve()
        root.mkdir(parents=True, exist_ok=True)
        return root

    async def _ensure_runtime(self, session: dict[str, Any]) -> dict[str, Any] | None:
        runtime = self._runtimes.get(session["id"])
        if runtime and runtime["proc"].poll() is None:
            return runtime
        available, reason = _tools_available()
        if not available:
            session["status"] = "degraded"
            session["degraded_reason"] = reason
            await self._save_session(session)
            return None
        try:
            return await self._launch_display(session)
        except Exception as exc:
            session["status"] = "degraded"
            session["degraded_reason"] = f"Xvfb launch failed: {exc}"
            await self._save_session(session)
            return None

    async def _launch_display(self, session: dict[str, Any]) -> dict[str, Any]:
        display_num = self._alloc_display()
        display = f":{display_num}"
        proc = subprocess.Popen(  # noqa: S603 — fixed Xvfb invocation
            [
                "Xvfb",
                display,
                "-screen",
                "0",
                f"{SCREEN_W}x{SCREEN_H}x{SCREEN_DEPTH}",
                "-nolisten",
                "tcp",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        lock = Path(f"/tmp/.X{display_num}-lock")
        waited = 0.0
        while waited < _LAUNCH_TIMEOUT_S:
            if proc.poll() is not None:
                raise RuntimeError("Xvfb exited during startup")
            if lock.exists():
                break
            await asyncio.sleep(0.2)
            waited += 0.2
        runtime = {"display": display, "display_num": display_num, "proc": proc, "apps": []}
        self._runtimes[session["id"]] = runtime
        session["display"] = display
        session["status"] = "active"
        session["degraded_reason"] = None
        await self._save_session(session)
        return runtime

    def _alloc_display(self) -> int:
        used = {rt["display_num"] for rt in self._runtimes.values()}
        n = _DISPLAY_BASE
        while n in used or Path(f"/tmp/.X{n}-lock").exists():
            n += 1
        return n

    async def _run(self, cmd: list[str], display: str) -> tuple[int, str, str]:
        env = {**os.environ, "DISPLAY": display}
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        return proc.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")

    async def _teardown_runtime(self, session_id: str) -> None:
        runtime = self._runtimes.pop(session_id, None)
        if not runtime:
            return
        for app in runtime.get("apps", []):
            try:
                app.terminate()
            except Exception:
                pass
        try:
            runtime["proc"].terminate()
        except Exception:
            pass

    # ── Persistence + audit ────────────────────────────────────────────────
    async def _load_or_create(
        self, args: dict[str, Any], *, organization_id: str, task_id: str | None
    ) -> dict[str, Any]:
        session_id = str(args.get("session_id") or "")
        if session_id:
            return await self._load_session(session_id, organization_id)
        return await self.create_session(
            organization_id=organization_id,
            member_id=str(args.get("member_id") or "chronos"),
            task_id=task_id,
            purpose=str(args.get("purpose") or "desktop task"),
            consent=args.get("consent") if isinstance(args.get("consent"), dict) else {},
        )

    async def _load_session(self, session_id: str, organization_id: str) -> dict[str, Any]:
        try:
            table = await reflect_table("desktop_sessions")
            async with engine.begin() as conn:
                row = (
                    await conn.execute(
                        select(table).where(table.c.id == session_id, table.c.organization_id == organization_id)
                    )
                ).mappings().first()
            if not row:
                raise KeyError(session_id)
            merged = _coerce_session(dict(row))
            # Preserve any live runtime fields the DB row does not carry.
            cached = self._sessions.get(session_id)
            if cached:
                merged["display"] = cached.get("display") or merged.get("display")
            self._sessions[session_id] = merged
            return merged
        except KeyError:
            raise
        except Exception:
            session = self._sessions.get(session_id)
            if not session or session["organization_id"] != organization_id:
                raise KeyError(session_id)
            return session

    async def _save_session(self, session: dict[str, Any]) -> None:
        session["updated_at"] = _now()
        self._sessions[session["id"]] = session
        try:
            table = await reflect_table("desktop_sessions")
            values = {
                key: session.get(key)
                for key in table.c.keys()
                if key in session and key not in {"created_at", "updated_at"}
            }
            async with engine.begin() as conn:
                existing = (await conn.execute(select(table.c.id).where(table.c.id == session["id"]))).first()
                if existing:
                    await conn.execute(update(table).where(table.c.id == session["id"]).values(**values, updated_at=_now()))
                else:
                    await conn.execute(insert(table).values(**values))
        except Exception:
            return

    async def _record_action(self, session: dict[str, Any], action: str, payload: dict[str, Any]) -> None:
        history = list(session.get("history") or [])
        history.append({"action": action, "payload": payload, "created_at": _stamp()})
        session["history"] = history[-100:]
        await self._save_session(session)
        await self._record_event(session, "desktop_action", {"action": action, **payload})

    async def _record_event(self, session: dict[str, Any], event_type: str, payload: dict[str, Any]) -> None:
        event_payload = {
            "type": event_type,
            "session_id": session["id"],
            "task_id": session.get("task_id"),
            **payload,
        }
        try:
            table = await reflect_table("desktop_session_events")
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
                        payload=event_payload,
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
                resource_type="tasks" if session.get("task_id") else "desktop_sessions",
                resource_id=session.get("task_id") or session["id"],
                payload=event_payload,
            )
        except Exception:
            pass


desktop_connector = DesktopConnector()
