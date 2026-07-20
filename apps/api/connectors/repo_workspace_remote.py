"""Production repository workspaces backed by persistent isolated E2B sandboxes."""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import shlex
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote

import httpx
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from connectors.e2b_runtime import (
    SANDBOX_ROOT,
    RuntimeUnavailable,
    SandboxExpired,
    SandboxRuntime,
    repo_runtime,
)
from connectors.github_repo_publisher import (
    GitHubPublicationError as RepoPublicationError,
    GitHubRepoPublisher,
)
from core import audit
from core.config import settings
from core.db import engine, reflect_table
from core.models import ToolResult
from core.object_storage import delete_object, get_object, put_object


MAX_FILE_BYTES = 256_000
LEASE_SECONDS = 660
GITHUB_API_VERSION = "2026-03-10"
GITHUB_API_BASE = "https://api.github.com"
CONTROL_ROOT = f"{SANDBOX_ROOT}/.chronos-control"
RUNNER_PATH = f"{CONTROL_ROOT}/repo_runner.py"
RUNNER_SOURCE = Path(__file__).with_name("repo_sandbox_runner.py")
SHELL_METACHARS = {";", "&", "|", "`", "$", "<", ">"}


class RepoWorkspaceBusy(RuntimeError):
    pass


class RepoWorkspaceUnavailable(RuntimeError):
    pass


class RemoteActionFailed(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    return (value or _now()).isoformat()


def _repo_path(value: Any, *, default: str) -> str:
    raw = str(value or default).strip().replace("\\", "/")
    path = PurePosixPath(raw)
    if (
        not raw
        or path.is_absolute()
        or ".." in path.parts
        or any(ord(char) < 32 for char in raw)
        or len(raw) > 240
        or path.parts[0] == ".chronos-control"
    ):
        raise ValueError("repo_path must be a bounded workspace-relative path")
    return str(path)


def _validate_branch_name(branch: str) -> str:
    branch = branch.strip()
    if (
        not branch
        or branch.startswith(("-", "."))
        or branch.endswith((".", "/"))
        or ".." in branch
        or "@{" in branch
        or re.search(r"[\s~^:?*\[\\]", branch)
    ):
        raise ValueError("Invalid branch name")
    return branch


def _validate_github_url(url: str) -> tuple[str, str, str]:
    value = url.strip()
    match = re.fullmatch(
        r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?",
        value,
    )
    if not match:
        raise ValueError("Only https://github.com/<owner>/<repo> URLs are supported")
    owner, repo = match.groups()
    return value.rstrip("/"), owner, repo


def _safe_pytest_command(args: dict[str, Any]) -> list[str]:
    raw = str(args.get("command") or "").strip()
    if not raw:
        return ["pytest", "-q"]
    if any(character in raw for character in SHELL_METACHARS):
        raise ValueError("Only pytest commands without shell operators are allowed")
    tokens = shlex.split(raw)
    if not tokens or tokens[0] != "pytest":
        raise ValueError("Only pytest commands are allowed")
    for token in tokens[1:]:
        if (
            any(character in token for character in SHELL_METACHARS)
            or token.startswith("/")
            or token == ".."
            or token.startswith("../")
            or "/../" in token
        ):
            raise ValueError("Unsafe pytest path or shell operator")
    return tokens


def _bounded_timeout(value: Any, *, default: int = 60) -> int:
    try:
        parsed = int(value or default)
    except (TypeError, ValueError):
        parsed = default
    return max(1, min(parsed, settings.e2b_repo_command_timeout_seconds))


def _public(workspace: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(workspace["id"]),
        "repo_path": str(workspace["repo_path"]),
        "status": str(workspace.get("status") or "active"),
        "runtime": "isolated_e2b",
        "persistent": True,
        "resumable": bool(workspace.get("snapshot_object_key")),
        "snapshot_version": int(workspace.get("snapshot_version") or 0),
        "last_used_at": workspace.get("last_used_at"),
    }


class ProductionRepoWorkspaceConnector:
    def __init__(self) -> None:
        self._runtime: SandboxRuntime | None = None
        self._github_transport: httpx.AsyncBaseTransport | None = None

    def _get_runtime(self) -> SandboxRuntime | None:
        return self._runtime if self._runtime is not None else repo_runtime()

    async def close_task_workspaces(
        self,
        *,
        organization_id: str,
        task_ids: list[str],
    ) -> dict[str, Any]:
        """Terminate only repository sandboxes bound to the cancelled tasks.

        A live workspace lease is respected: cleanup retries after the command
        holder exits instead of killing a sandbox while a peer is snapshotting.
        Provider ownership metadata is verified before every kill.
        """

        scoped_ids = {str(task_id) for task_id in task_ids if task_id}
        if not scoped_ids:
            return {"status": "complete", "closed": 0}
        table = await reflect_table("repo_workspaces")
        async with engine.begin() as conn:
            rows = (
                await conn.execute(
                    select(table).where(
                        table.c.organization_id == organization_id,
                        table.c.task_id.in_(sorted(scoped_ids)),
                        table.c.status != "closed",
                    )
                )
            ).mappings().all()
        runtime = self._get_runtime()
        failures: list[str] = []
        closed = 0
        for raw in rows:
            workspace = dict(raw)
            lease_id = f"cleanup:{uuid.uuid4().hex}"
            try:
                workspace = await self._claim_workspace(workspace, lease_id)
                sandbox_id = str(workspace.get("sandbox_id") or "")
                if sandbox_id:
                    if runtime is None:
                        raise RuntimeUnavailable("repo cancellation requires the E2B runtime")
                    try:
                        await runtime.resume(
                            sandbox_id,
                            timeout_seconds=settings.e2b_repo_timeout_seconds,
                            expected_metadata={
                                "org": str(workspace["organization_id"]),
                                "task": str(workspace["task_id"]),
                                "workspace": str(workspace["id"]),
                                "profile": "repo",
                            },
                        )
                        await runtime.kill(sandbox_id)
                    except SandboxExpired:
                        pass
                await self._update_workspace(
                    str(workspace["id"]),
                    status="closed",
                    sandbox_id=None,
                    expires_at=_now(),
                    lease_owner=None,
                    lease_expires_at=None,
                )
                closed += 1
                await audit.log(
                    "activity",
                    "chronos",
                    "repo.workspace_cancelled",
                    organization_id=organization_id,
                    resource_type="repo_workspaces",
                    resource_id=str(workspace["id"]),
                    payload={"task_id": str(workspace["task_id"])},
                )
            except Exception:
                failures.append(str(workspace["id"]))
            finally:
                await self._release_workspace(str(workspace["id"]), lease_id)
        if failures:
            raise RepoWorkspaceUnavailable(
                f"repo provider cleanup failed for {len(failures)} workspace(s)"
            )
        return {"status": "complete", "closed": closed}

    async def execute(self, tool: str, args: dict[str, Any]) -> ToolResult:
        args = dict(args)
        args.pop("__connector_tier", None)
        org_id = str(args.pop("__org_id", "") or "")
        task_id = str(args.pop("__task_id", "") or "")
        member_id = str(args.pop("__member_id", "") or "")
        if not org_id or not task_id:
            return self._unavailable("tenant_task_scope_required")
        runtime = self._get_runtime()
        if runtime is None:
            return self._unavailable("repo_runtime_not_configured")

        action = tool.partition(".")[2]
        default_path = "repos/imported" if action == "clone" else "repos/python_bug"
        repo_path = _repo_path(args.get("repo_path"), default=default_path)
        if action == "clone":
            if str(args.get("source_path") or "").strip():
                raise ValueError(
                    "source_path is development-only; production clone requires a GitHub HTTPS URL"
                )
            _validate_github_url(str(args.get("source_url") or ""))

        try:
            if action in {"clone", "open_fixture"}:
                workspace = await self._load_or_create_workspace(
                    org_id=org_id,
                    task_id=task_id,
                    member_id=member_id or None,
                    repo_path=repo_path,
                )
            else:
                workspace = await self._load_workspace(org_id, task_id, repo_path)
                if workspace is None:
                    raise RepoWorkspaceUnavailable("repo_workspace_not_found")
            lease_id = uuid.uuid4().hex
            workspace = await self._claim_workspace(workspace, lease_id)
        except RepoWorkspaceBusy:
            return ToolResult(
                data={
                    "status": "busy",
                    "reason": "Another replica is currently operating on this repository workspace.",
                    "execution_boundary": "isolated_runtime",
                    "host_execution": False,
                },
                summary="Repository workspace is busy; retry safely",
            )
        except RepoWorkspaceUnavailable as exc:
            return self._unavailable(str(exc))

        try:
            workspace = await self._ensure_sandbox(runtime, workspace)
            if action == "clone":
                result = await self._clone(runtime, workspace, args, org_id=org_id, member_id=member_id)
                workspace = await self._snapshot(runtime, workspace)
            elif action == "open_fixture":
                result = await self._open_fixture(runtime, workspace, args)
                workspace = await self._snapshot(runtime, workspace)
            elif action == "create_branch":
                result = await self._remote_action(
                    runtime,
                    workspace,
                    {"action": action, "repo_path": repo_path, "branch": _validate_branch_name(str(args.get("branch") or ""))},
                )
                workspace = await self._snapshot(runtime, workspace)
            elif action == "list_files":
                result = await self._remote_action(runtime, workspace, {"action": action, "repo_path": repo_path})
            elif action == "read_file":
                result = await self._remote_action(
                    runtime,
                    workspace,
                    {"action": action, "repo_path": repo_path, "path": str(args.get("path") or "")},
                )
            elif action == "write_file":
                raw = str(args.get("content") or "").encode("utf-8")
                if len(raw) > MAX_FILE_BYTES:
                    raise ValueError(f"repo.write_file payload exceeds {MAX_FILE_BYTES} bytes")
                result = await self._remote_action(
                    runtime,
                    workspace,
                    {
                        "action": action,
                        "repo_path": repo_path,
                        "path": str(args.get("path") or ""),
                        "content_b64": base64.b64encode(raw).decode("ascii"),
                    },
                )
                workspace = await self._snapshot(runtime, workspace)
            elif action == "run_tests":
                command = _safe_pytest_command(args)
                result = await self._remote_action(
                    runtime,
                    workspace,
                    {
                        "action": action,
                        "repo_path": repo_path,
                        "command": command,
                        "timeout_seconds": _bounded_timeout(args.get("timeout_seconds"), default=60),
                    },
                    timeout_seconds=_bounded_timeout(args.get("timeout_seconds"), default=60) + 15,
                )
            elif action in {"diff", "status"}:
                result = await self._remote_action(runtime, workspace, {"action": action, "repo_path": repo_path})
            elif action == "commit":
                message = str(args.get("message") or "").strip()
                if not message or len(message.encode("utf-8")) > 1000:
                    raise ValueError("Commit message is required and must be at most 1000 bytes")
                result = await self._remote_action(
                    runtime,
                    workspace,
                    {"action": action, "repo_path": repo_path, "message": message},
                )
                workspace = await self._snapshot(runtime, workspace)
            elif action == "create_pr":
                result, workspace = await self._create_pr(
                    runtime,
                    workspace,
                    args,
                    org_id=org_id,
                    task_id=task_id,
                    member_id=member_id,
                )
                workspace = await self._snapshot(runtime, workspace)
            elif action == "review":
                result = await self._remote_action(
                    runtime,
                    workspace,
                    {
                        "action": action,
                        "repo_path": repo_path,
                        "title": str(args.get("title") or "Code review")[:500],
                        "created_at": _iso(),
                    },
                )
                workspace = await self._snapshot(runtime, workspace)
            else:
                raise ValueError(f"Unknown repo tool: {tool}")

            workspace = await self._update_workspace(
                workspace["id"],
                status="active",
                last_used_at=_now(),
                expires_at=_now() + timedelta(seconds=settings.e2b_repo_timeout_seconds),
            )
            await audit.log(
                "activity",
                member_id or "chronos",
                f"repo.{action}",
                organization_id=org_id,
                resource_type="repo_workspaces",
                resource_id=str(workspace["id"]),
                payload={
                    "task_id": task_id,
                    "repo_path_hash": hashlib.sha256(repo_path.encode()).hexdigest(),
                    "snapshot_version": int(workspace.get("snapshot_version") or 0),
                    "execution_boundary": "isolated_runtime",
                },
            )
            return ToolResult(
                data={
                    **result,
                    "workspace": _public(workspace),
                    "execution_boundary": "isolated_runtime",
                    "host_execution": False,
                },
                summary=self._summary(action, result),
            )
        except (RuntimeUnavailable, SandboxExpired):
            await self._update_workspace(workspace["id"], status="failed")
            return self._unavailable("repo_runtime_unavailable")
        except RemoteActionFailed as exc:
            await self._update_workspace(workspace["id"], status="failed")
            return ToolResult(
                data={
                    "status": "failure",
                    "error_code": exc.code,
                    "reason": "The isolated repository operation failed; no provider or credential details were exposed.",
                    "workspace": _public(workspace),
                    "execution_boundary": "isolated_runtime",
                    "host_execution": False,
                },
                summary=f"Repository {action} failed in isolated runtime",
            )
        except RepoPublicationError as exc:
            return ToolResult(
                data={
                    "status": "failure",
                    "error_code": exc.code,
                    "reason": self._publication_error_reason(exc.code),
                    "workspace": _public(workspace),
                    "execution_boundary": "isolated_runtime",
                    "host_execution": False,
                },
                summary="GitHub pull request publication did not complete",
            )
        except RepoWorkspaceUnavailable as exc:
            await self._update_workspace(workspace["id"], status="failed")
            return self._unavailable(str(exc))
        finally:
            await self._release_workspace(workspace["id"], lease_id)

    def _unavailable(self, code: str) -> ToolResult:
        messages = {
            "tenant_task_scope_required": "Production repository workspaces require explicit tenant and task scope.",
            "repo_runtime_not_configured": "Persistent isolated repository runtime is not configured.",
            "repo_workspace_not_found": "Repository workspace was not initialized for this task.",
            "repo_runtime_unavailable": "Persistent isolated repository runtime is temporarily unavailable.",
            "workspace_quota_exceeded": "Repository workspace quota is exhausted for this tenant or task.",
        }
        return ToolResult(
            data={
                "status": "unavailable",
                "error_code": code,
                "reason": messages.get(code, "Repository workspace is unavailable."),
                "execution_boundary": "isolated_runtime_required",
                "host_execution": False,
            },
            summary="Repository workspace unavailable",
        )

    @staticmethod
    def _summary(action: str, result: dict[str, Any]) -> str:
        if action == "run_tests":
            return f"Repo tests {result.get('status') or 'failure'} in isolated runtime"
        if action == "list_files":
            return f"Listed {result.get('count', 0)} isolated repo files"
        if action == "read_file":
            return f"Read {result.get('path') or 'repo file'} from isolated workspace"
        if action == "write_file":
            return f"Wrote {result.get('path') or 'repo file'} in isolated workspace"
        return f"Repository {action} completed in isolated runtime"

    @staticmethod
    def _publication_error_reason(code: str) -> str:
        reasons = {
            "approval_evidence_required": "Verified internal approval evidence is required.",
            "idempotency_evidence_required": "Durable idempotency evidence is required.",
            "github_connection_required": "Connect the current member's direct GitHub OAuth account.",
            "github_scope_unverifiable": "GitHub did not return verifiable OAuth scope evidence.",
            "github_repo_scope_required": "The connected GitHub token lacks the repo scope required to write this repository.",
            "github_public_repo_scope_required": "The connected GitHub token lacks repo or public_repo write scope.",
            "github_workflow_scope_required": "Publishing workflow-file changes additionally requires GitHub's workflow OAuth scope.",
            "github_push_not_authorized": "The connected GitHub user does not have push access to this repository.",
            "github_base_not_found": "The approved base branch does not exist.",
            "github_base_moved": "The GitHub base branch changed after this workspace was cloned; re-clone and re-apply the change.",
            "github_head_exists": "The approved head branch already exists and is not owned by this Chronos publication.",
            "github_branch_changed": "The GitHub head branch changed during publication; no force push was attempted.",
            "github_provider_unavailable": "GitHub was unavailable or returned an invalid response.",
            "github_provider_rejected": "GitHub rejected the branch, commit, or pull request operation.",
            "github_publish_payload_invalid": "The committed workspace change set is empty, dirty, too large, or does not match the approved head branch.",
            "github_publication_conflict": "Stored publication evidence does not match this approved request.",
        }
        return reasons.get(code, "GitHub pull request publication failed safely.")

    async def _load_or_create_workspace(
        self,
        *,
        org_id: str,
        task_id: str,
        member_id: str | None,
        repo_path: str,
    ) -> dict[str, Any]:
        existing = await self._load_workspace(org_id, task_id, repo_path)
        if existing is not None:
            return existing
        table = await reflect_table("repo_workspaces")
        async with engine.begin() as conn:
            quota_active = and_(
                table.c.status.in_(["creating", "active", "recovering"]),
                or_(table.c.expires_at.is_(None), table.c.expires_at > _now()),
            )
            org_count = (
                await conn.execute(
                    select(func.count()).select_from(table).where(
                        table.c.organization_id == org_id,
                        quota_active,
                    )
                )
            ).scalar_one()
            task_count = (
                await conn.execute(
                    select(func.count()).select_from(table).where(
                        table.c.organization_id == org_id,
                        table.c.task_id == task_id,
                        quota_active,
                    )
                )
            ).scalar_one()
            if (
                int(org_count) >= settings.e2b_repo_max_workspaces_per_org
                or int(task_count) >= settings.e2b_repo_max_workspaces_per_task
            ):
                raise RepoWorkspaceUnavailable("workspace_quota_exceeded")
            workspace_id = str(uuid.uuid4())
            row = (
                await conn.execute(
                    pg_insert(table)
                    .values(
                        id=workspace_id,
                        organization_id=org_id,
                        region=settings.region,
                        task_id=task_id,
                        member_id=member_id,
                        repo_path=repo_path,
                        runtime_provider="e2b",
                        status="creating",
                        metadata={"network": "repo_profile", "host_execution": False},
                    )
                    .on_conflict_do_nothing(
                        index_elements=["organization_id", "task_id", "repo_path"]
                    )
                    .returning(table)
                )
            ).mappings().first()
        if row:
            return dict(row)
        raced = await self._load_workspace(org_id, task_id, repo_path)
        if raced is None:
            raise RepoWorkspaceUnavailable("repo_workspace_create_failed")
        return raced

    async def _load_workspace(self, org_id: str, task_id: str, repo_path: str) -> dict[str, Any] | None:
        table = await reflect_table("repo_workspaces")
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    select(table).where(
                        table.c.organization_id == org_id,
                        table.c.task_id == task_id,
                        table.c.repo_path == repo_path,
                        table.c.status != "closed",
                    )
                )
            ).mappings().first()
        return dict(row) if row else None

    async def _claim_workspace(self, workspace: dict[str, Any], lease_id: str) -> dict[str, Any]:
        table = await reflect_table("repo_workspaces")
        now = _now()
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    update(table)
                    .where(
                        table.c.id == workspace["id"],
                        or_(
                            table.c.lease_owner.is_(None),
                            table.c.lease_expires_at.is_(None),
                            table.c.lease_expires_at <= now,
                            table.c.lease_owner == lease_id,
                        ),
                    )
                    .values(
                        lease_owner=lease_id,
                        lease_expires_at=now
                        + timedelta(
                            seconds=max(
                                LEASE_SECONDS,
                                settings.e2b_repo_command_timeout_seconds + 300,
                            )
                        ),
                    )
                    .returning(table)
                )
            ).mappings().first()
        if not row:
            raise RepoWorkspaceBusy(str(workspace["id"]))
        return dict(row)

    async def _release_workspace(self, workspace_id: str, lease_id: str) -> None:
        try:
            table = await reflect_table("repo_workspaces")
            async with engine.begin() as conn:
                await conn.execute(
                    update(table)
                    .where(table.c.id == workspace_id, table.c.lease_owner == lease_id)
                    .values(lease_owner=None, lease_expires_at=None)
                )
        except Exception:
            # Lease expiry is the cross-replica cleanup backstop.
            return

    async def _update_workspace(self, workspace_id: str, **values: Any) -> dict[str, Any]:
        table = await reflect_table("repo_workspaces")
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    update(table)
                    .where(table.c.id == workspace_id)
                    .values(**values, updated_at=_now())
                    .returning(table)
                )
            ).mappings().one()
        return dict(row)

    async def _ensure_sandbox(
        self, runtime: SandboxRuntime, workspace: dict[str, Any]
    ) -> dict[str, Any]:
        sandbox_id = str(workspace.get("sandbox_id") or "")
        if sandbox_id:
            try:
                resume = getattr(runtime, "resume", None)
                if resume is not None:
                    await resume(
                        sandbox_id,
                        timeout_seconds=settings.e2b_repo_timeout_seconds,
                        expected_metadata={
                            "org": str(workspace["organization_id"]),
                            "task": str(workspace["task_id"]),
                            "workspace": str(workspace["id"]),
                            "profile": "repo",
                        },
                    )
                else:
                    keepalive = getattr(runtime, "keepalive", None)
                    if keepalive is None:
                        raise RuntimeUnavailable("repo runtime cannot resume persistent sandboxes")
                    await keepalive(
                        sandbox_id,
                        timeout_seconds=settings.e2b_repo_timeout_seconds,
                    )
                await self._install_runner(runtime, sandbox_id)
                return workspace
            except SandboxExpired:
                workspace = await self._update_workspace(
                    workspace["id"], status="recovering", sandbox_id=None
                )

        sandbox_id = await runtime.create(
            timeout_seconds=settings.e2b_repo_timeout_seconds,
            metadata={
                "org": str(workspace["organization_id"]),
                "task": str(workspace["task_id"]),
                "workspace": str(workspace["id"]),
                "profile": "repo",
            },
        )
        await self._install_runner(runtime, sandbox_id)
        workspace = await self._update_workspace(
            workspace["id"],
            sandbox_id=sandbox_id,
            status="recovering" if workspace.get("snapshot_object_key") else "active",
            expires_at=_now() + timedelta(seconds=settings.e2b_repo_timeout_seconds),
        )
        object_key = str(workspace.get("snapshot_object_key") or "")
        if object_key:
            snapshot = await get_object(object_key)
            if len(snapshot) > settings.e2b_repo_max_snapshot_bytes:
                raise RepoWorkspaceUnavailable("snapshot_quota_exceeded")
            archive_path = f"{CONTROL_ROOT}/{uuid.uuid4().hex}.tar.gz"
            await runtime.write(sandbox_id, archive_path, snapshot)
            await self._remote_action(
                runtime,
                workspace,
                {
                    "action": "restore",
                    "repo_path": str(workspace["repo_path"]),
                    "archive_path": archive_path.removeprefix(f"{SANDBOX_ROOT}/"),
                },
            )
            await self._remove(runtime, sandbox_id, archive_path)
            workspace = await self._update_workspace(workspace["id"], status="active")
        return workspace

    async def _install_runner(self, runtime: SandboxRuntime, sandbox_id: str) -> None:
        result = await runtime.run(
            sandbox_id,
            f"mkdir -p {CONTROL_ROOT}",
            cwd=SANDBOX_ROOT,
            timeout_seconds=10,
        )
        if result.get("status") != "success":
            raise RuntimeUnavailable("repo control directory initialization failed")
        await runtime.write(sandbox_id, RUNNER_PATH, RUNNER_SOURCE.read_bytes())

    async def _remote_action(
        self,
        runtime: SandboxRuntime,
        workspace: dict[str, Any],
        spec: dict[str, Any],
        *,
        timeout_seconds: int | None = None,
    ) -> dict[str, Any]:
        sandbox_id = str(workspace["sandbox_id"])
        request_id = uuid.uuid4().hex
        spec_path = f"{CONTROL_ROOT}/{request_id}.json"
        output_path = f"{CONTROL_ROOT}/{request_id}.out.json"
        # Repo tests can execute arbitrary tenant code inside the sandbox. Put a
        # fresh trusted runner in place for every control action so an earlier
        # process cannot persistently replace the API-to-sandbox command shim.
        await runtime.write(sandbox_id, RUNNER_PATH, RUNNER_SOURCE.read_bytes())
        await runtime.write(
            sandbox_id,
            spec_path,
            json.dumps(spec, separators=(",", ":"), ensure_ascii=False).encode("utf-8"),
        )
        # Only Chronos-generated fixed paths enter this command. Every model or
        # user value remains in the JSON control file and reaches subprocess as
        # argv inside the isolated runner.
        command = f"python3 {RUNNER_PATH} {spec_path} {output_path}"
        try:
            run_result = await runtime.run(
                sandbox_id,
                command,
                cwd=SANDBOX_ROOT,
                timeout_seconds=min(
                    timeout_seconds or settings.e2b_repo_command_timeout_seconds,
                    settings.e2b_repo_command_timeout_seconds + 15,
                ),
            )
            if run_result.get("status") == "timeout":
                raise RemoteActionFailed("timeout")
            raw = await runtime.read(sandbox_id, output_path)
            envelope = json.loads(raw.decode("utf-8"))
            if not envelope.get("ok") or not isinstance(envelope.get("data"), dict):
                raise RemoteActionFailed(str(envelope.get("error_code") or "remote_action_failed"))
            return dict(envelope["data"])
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise RemoteActionFailed("invalid_runtime_response") from exc
        finally:
            await self._remove(runtime, sandbox_id, spec_path)
            await self._remove(runtime, sandbox_id, output_path)

    async def _remove(self, runtime: SandboxRuntime, sandbox_id: str, path: str) -> None:
        remove = getattr(runtime, "remove", None)
        if remove is None:
            return
        try:
            await remove(sandbox_id, path)
        except Exception:
            return

    async def _snapshot(
        self, runtime: SandboxRuntime, workspace: dict[str, Any]
    ) -> dict[str, Any]:
        sandbox_id = str(workspace["sandbox_id"])
        version = int(workspace.get("snapshot_version") or 0) + 1
        archive_path = f"{CONTROL_ROOT}/{uuid.uuid4().hex}.tar.gz"
        await self._remote_action(
            runtime,
            workspace,
            {
                "action": "archive",
                "repo_path": str(workspace["repo_path"]),
                "archive_path": archive_path.removeprefix(f"{SANDBOX_ROOT}/"),
            },
        )
        raw = await runtime.read(sandbox_id, archive_path)
        await self._remove(runtime, sandbox_id, archive_path)
        if len(raw) > settings.e2b_repo_max_snapshot_bytes:
            raise RepoWorkspaceUnavailable("snapshot_quota_exceeded")
        tenant_hash = hashlib.sha256(str(workspace["organization_id"]).encode()).hexdigest()[:24]
        object_key = f"repo-workspaces/{tenant_hash}/{workspace['id']}/snapshot-{version}.tar.gz"
        await put_object(object_key, raw, "application/gzip")
        previous = str(workspace.get("snapshot_object_key") or "")
        updated = await self._update_workspace(
            workspace["id"],
            snapshot_object_key=object_key,
            snapshot_bytes=len(raw),
            snapshot_version=version,
            status="active",
        )
        if previous and previous != object_key:
            try:
                await delete_object(previous)
            except Exception:
                pass
        return updated

    async def _github_vault_ref(self, org_id: str, member_id: str) -> str | None:
        if not member_id:
            return None
        table = await reflect_table("connectors")
        filters = [
            table.c.organization_id == org_id,
            table.c.provider == "github",
            table.c.status == "active",
            table.c.vault_ref.like("vlt_%"),
        ]
        if "member_id" in table.c:
            filters.append(or_(table.c.member_id == member_id, table.c.member_id.is_(None)))
        async with engine.begin() as conn:
            ref = (
                await conn.execute(
                    select(table.c.vault_ref).where(*filters).order_by(
                        table.c.member_id.desc().nullslast() if "member_id" in table.c else table.c.id
                    ).limit(1)
                )
            ).scalar_one_or_none()
        value = str(ref or "")
        return value if value.startswith("vlt_") else None

    async def _download_private_archive(
        self,
        *,
        org_id: str,
        member_id: str,
        owner: str,
        repo: str,
        ref: str,
    ) -> tuple[bytes, str] | None:
        vault_ref = await self._github_vault_ref(org_id, member_id)
        if not vault_ref:
            return None
        token = await self._github_token(org_id=org_id, vault_ref=vault_ref)
        if not token:
            return None
        url = f"https://api.github.com/repos/{owner}/{repo}/tarball/{ref}"
        chunks: list[bytes] = []
        total = 0
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(45.0), follow_redirects=True
            ) as client:
                commit_response = await client.get(
                    f"{GITHUB_API_BASE}/repos/{owner}/{repo}/commits/{quote(ref, safe='')}",
                    headers={
                        "Accept": "application/vnd.github+json",
                        "Authorization": f"Bearer {token}",
                        "X-GitHub-Api-Version": GITHUB_API_VERSION,
                        "User-Agent": "Chronos-Repo-Workspace",
                    },
                )
                if commit_response.status_code == 404:
                    return None
                if commit_response.status_code != 200:
                    raise RepoWorkspaceUnavailable("github_archive_unavailable")
                commit_payload = commit_response.json()
                remote_sha = str(
                    commit_payload.get("sha") if isinstance(commit_payload, dict) else ""
                )
                if re.fullmatch(r"[0-9a-f]{40}", remote_sha) is None:
                    raise RepoWorkspaceUnavailable("github_archive_unavailable")
                async with client.stream(
                    "GET",
                    url,
                    headers={
                        "Accept": "application/vnd.github+json",
                        "Authorization": f"Bearer {token}",
                        "X-GitHub-Api-Version": GITHUB_API_VERSION,
                        "User-Agent": "Chronos-Repo-Workspace",
                    },
                ) as response:
                    if response.status_code == 404:
                        return None
                    response.raise_for_status()
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > settings.e2b_repo_max_snapshot_bytes:
                            raise RepoWorkspaceUnavailable("source_archive_quota_exceeded")
                        chunks.append(chunk)
        except RepoWorkspaceUnavailable:
            raise
        except (httpx.HTTPError, OSError):
            # HTTPStatusError retains the originating request headers, including
            # Authorization. Replace it without a chained exception so an error
            # reporter cannot serialize the GitHub token.
            raise RepoWorkspaceUnavailable("github_archive_unavailable") from None
        return b"".join(chunks), remote_sha

    async def _github_token(self, *, org_id: str, vault_ref: str) -> str:
        from connectors.vault import get as vault_get

        try:
            credentials = await vault_get(vault_ref, org_id=org_id)
        except Exception:
            raise RepoPublicationError("github_connection_required") from None
        token = str(credentials.get("access_token") or "")
        if not token:
            raise RepoPublicationError("github_connection_required")
        return token

    async def _clone(
        self,
        runtime: SandboxRuntime,
        workspace: dict[str, Any],
        args: dict[str, Any],
        *,
        org_id: str,
        member_id: str,
    ) -> dict[str, Any]:
        source_url, owner, repo = _validate_github_url(str(args.get("source_url") or ""))
        ref = _validate_branch_name(str(args.get("ref") or "HEAD"))
        archive = await self._download_private_archive(
            org_id=org_id,
            member_id=member_id,
            owner=owner,
            repo=repo,
            ref=ref,
        )
        if archive is not None:
            archive_bytes, remote_source_sha = archive
            archive_path = f"{CONTROL_ROOT}/{uuid.uuid4().hex}.tar.gz"
            await runtime.write(str(workspace["sandbox_id"]), archive_path, archive_bytes)
            result = await self._remote_action(
                runtime,
                workspace,
                {
                    "action": "extract_archive",
                    "repo_path": str(workspace["repo_path"]),
                    "archive_path": archive_path.removeprefix(f"{SANDBOX_ROOT}/"),
                    "strip_first": True,
                    "message": "Import authenticated GitHub repository snapshot",
                },
            )
            await self._remove(runtime, str(workspace["sandbox_id"]), archive_path)
            source = {"type": "github_oauth_snapshot", "url": source_url, "ref": ref}
        else:
            result = await self._remote_action(
                runtime,
                workspace,
                {
                    "action": "clone",
                    "repo_path": str(workspace["repo_path"]),
                    "source_url": source_url,
                    "ref": ref,
                    "timeout_seconds": _bounded_timeout(args.get("timeout_seconds"), default=60),
                },
                timeout_seconds=_bounded_timeout(args.get("timeout_seconds"), default=60) + 15,
            )
            source = {"type": "github_public", "url": source_url, "ref": ref}
            remote_source_sha = str(result.get("sha") or "")
        await self._update_workspace(
            workspace["id"],
            source_type=source["type"],
            source_url=source_url,
            metadata={
                "network": "repo_profile",
                "host_execution": False,
                "credential_transport": "api_archive" if archive is not None else "none",
                "source_owner": owner,
                "source_repo": repo,
                "source_ref": ref,
                "source_sha": remote_source_sha,
                "local_base_sha": str(result.get("sha") or ""),
            },
        )
        return {
            "repo_path": str(workspace["repo_path"]),
            "branch": result.get("branch") or "main",
            "sha": result.get("sha"),
            "source": source,
            "files": result.get("files") or [],
        }

    async def _open_fixture(
        self,
        runtime: SandboxRuntime,
        workspace: dict[str, Any],
        args: dict[str, Any],
    ) -> dict[str, Any]:
        name = str(args.get("name") or "python_bug")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
            raise ValueError("Invalid fixture name")
        source = Path(__file__).resolve().parents[1] / "fixtures" / "repo_workspace" / name
        fixture_root = Path(__file__).resolve().parents[1] / "fixtures" / "repo_workspace"
        if fixture_root.resolve() not in source.resolve().parents or not source.is_dir():
            raise FileNotFoundError("fixture repo not found")
        total = 0
        for path in sorted(source.rglob("*")):
            if path.is_symlink() or not path.is_file():
                continue
            raw = path.read_bytes()
            total += len(raw)
            if len(raw) > MAX_FILE_BYTES or total > settings.e2b_repo_max_snapshot_bytes:
                raise ValueError("fixture exceeds repository workspace quota")
            rel = path.relative_to(source).as_posix()
            await self._remote_action(
                runtime,
                workspace,
                {
                    "action": "write_uninitialized",
                    "repo_path": str(workspace["repo_path"]),
                    "path": rel,
                    "content_b64": base64.b64encode(raw).decode("ascii"),
                },
            )
        result = await self._remote_action(
            runtime,
            workspace,
            {
                "action": "init",
                "repo_path": str(workspace["repo_path"]),
                "message": "Import fixture repository",
            },
        )
        await self._update_workspace(
            workspace["id"], source_type="fixture", source_url=None
        )
        return {
            "repo_path": str(workspace["repo_path"]),
            "branch": result.get("branch") or "main",
            "sha": result.get("sha"),
            "source": {"type": "fixture", "name": name},
            "files": result.get("files") or [],
        }

    async def _create_pr(
        self,
        runtime: SandboxRuntime,
        workspace: dict[str, Any],
        args: dict[str, Any],
        *,
        org_id: str,
        task_id: str,
        member_id: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        title = str(args.get("title") or "").strip()
        if not title or len(title.encode("utf-8")) > 500:
            raise ValueError("PR title is required and must be at most 500 bytes")
        if not bool(args.get("__approved_by_gate")) or not str(args.get("__approval_id") or ""):
            raise RepoPublicationError("approval_evidence_required")
        idempotency_key = str(args.get("__idempotency_key") or "")
        if not idempotency_key:
            raise RepoPublicationError("idempotency_evidence_required")
        base = _validate_branch_name(str(args.get("base") or "main"))
        head_raw = str(args.get("head") or "").strip()
        head = _validate_branch_name(head_raw) if head_raw else ""
        body = str(args.get("body") or "")
        if len(body.encode("utf-8")) > 20_000:
            raise ValueError("PR body must be at most 20000 bytes")

        metadata = dict(workspace.get("metadata") or {})
        owner = str(metadata.get("source_owner") or "")
        repo = str(metadata.get("source_repo") or "")
        source_sha = str(metadata.get("source_sha") or "")
        local_base_sha = str(metadata.get("local_base_sha") or "")
        if (
            not re.fullmatch(r"[A-Za-z0-9_.-]+", owner)
            or not re.fullmatch(r"[A-Za-z0-9_.-]+", repo)
            or re.fullmatch(r"[0-9a-f]{40}", source_sha) is None
            or re.fullmatch(r"[0-9a-f]{40}", local_base_sha) is None
        ):
            raise RepoPublicationError("github_publish_payload_invalid")

        changes = await self._remote_action(
            runtime,
            workspace,
            {
                "action": "publication_changes",
                "repo_path": str(workspace["repo_path"]),
                "base_sha": local_base_sha,
            },
        )
        current_branch = str(changes.get("branch") or "")
        if not current_branch:
            raise RepoPublicationError("github_publish_payload_invalid")
        current_branch = _validate_branch_name(current_branch)
        head = head or current_branch
        if head != current_branch or head == base:
            raise RepoPublicationError("github_publish_payload_invalid")
        if bool(changes.get("dirty")) or int(changes.get("file_count") or 0) <= 0:
            raise RepoPublicationError("github_publish_payload_invalid")
        additions = list(changes.get("additions") or [])
        deletions = list(changes.get("deletions") or [])
        if int(changes.get("file_count") or 0) != len(additions) + len(deletions):
            raise RepoPublicationError("github_publish_payload_invalid")
        self._validate_publication_changes(additions, deletions)
        change_hash = hashlib.sha256(
            json.dumps(
                {"additions": additions, "deletions": deletions},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()

        approval_id = str(args["__approval_id"])
        key_hash = hashlib.sha256(
            f"{org_id}:{task_id}:{workspace['id']}:{idempotency_key}".encode()
        ).hexdigest()
        request_hash = hashlib.sha256(
            json.dumps(
                {
                    "workspace_id": str(workspace["id"]),
                    "source_url": str(workspace.get("source_url") or ""),
                    "source_sha": source_sha,
                    "title": title,
                    "body": body,
                    "base": base,
                    "head": head,
                    "change_hash": change_hash,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        publications = dict(metadata.get("pr_publications") or {})
        existing = dict(publications.get(key_hash) or {})
        if existing and (
            existing.get("request_hash") != request_hash
            or existing.get("approval_id") != approval_id
        ):
            raise RepoPublicationError("github_publication_conflict")
        marker = str(existing.get("marker") or f"chronos-request:{key_hash[:24]}")
        state = existing or {
            "request_id": uuid.uuid4().hex,
            "request_hash": request_hash,
            "approval_id": approval_id,
            "idempotency_hash": key_hash,
            "stage": "claimed",
            "base": base,
            "head": head,
            "source_sha": source_sha,
            "marker": marker,
            "created_at": _iso(),
        }
        if not existing:
            workspace = await self._save_publication_state(workspace, key_hash, state)

        provider = self._provider_evidence(state)
        newly_published = False
        if provider is None:
            vault_ref = await self._github_vault_ref(org_id, member_id)
            if not vault_ref:
                raise RepoPublicationError("github_connection_required")
            token = await self._github_token(org_id=org_id, vault_ref=vault_ref)
            publisher = GitHubRepoPublisher(token, transport=self._github_transport)

            async def persist(next_state: dict[str, Any]) -> None:
                nonlocal workspace
                workspace = await self._save_publication_state(
                    workspace, key_hash, next_state
                )

            try:
                provider, state, newly_published = await publisher.publish(
                    owner=owner,
                    repo=repo,
                    base=base,
                    head=head,
                    source_sha=source_sha,
                    title=title,
                    body=body,
                    additions=additions,
                    deletions=deletions,
                    marker=marker,
                    key_hash=key_hash,
                    state=state,
                    persist=persist,
                )
            finally:
                publisher.clear_credentials()
                token = ""

        if newly_published:
            await audit.log(
                "activity",
                member_id,
                "repo.pr_published",
                organization_id=org_id,
                resource_type="repo_workspaces",
                resource_id=str(workspace["id"]),
                payload={
                    "task_id": task_id,
                    "approval_id": approval_id,
                    "idempotency_hash": key_hash,
                    "provider": "github",
                    "provider_pr_id": provider["id"],
                    "provider_node_id": provider["node_id"],
                    "provider_number": provider["number"],
                    "provider_commit_oid": provider["commit_oid"],
                    "url": provider["url"],
                },
                decision="published",
            )

        result = await self._remote_action(
            runtime,
            workspace,
            {
                "action": "create_pr",
                "repo_path": str(workspace["repo_path"]),
                "request_id": str(state["request_id"]),
                "title": title,
                "body": body,
                "base": base,
                "head": head,
                "approval_id": approval_id,
                "created_at": str(state.get("created_at") or _iso()),
                "provider": provider,
            },
        )
        return result, workspace

    @staticmethod
    def _validate_publication_changes(
        additions: list[Any], deletions: list[Any]
    ) -> None:
        if len(additions) + len(deletions) > 200:
            raise RepoPublicationError("github_publish_payload_invalid")
        seen: set[str] = set()
        total = 0
        for item in additions:
            if not isinstance(item, dict):
                raise RepoPublicationError("github_publish_payload_invalid")
            path = str(item.get("path") or "")
            parsed = PurePosixPath(path)
            if (
                not path
                or parsed.is_absolute()
                or ".." in parsed.parts
                or path.startswith(".chronos/")
                or path in seen
            ):
                raise RepoPublicationError("github_publish_payload_invalid")
            try:
                raw = base64.b64decode(str(item.get("contents") or ""), validate=True)
            except (binascii.Error, ValueError, TypeError):
                raise RepoPublicationError("github_publish_payload_invalid") from None
            if len(raw) > 1_048_576:
                raise RepoPublicationError("github_publish_payload_invalid")
            total += len(raw)
            if total > 10_485_760:
                raise RepoPublicationError("github_publish_payload_invalid")
            seen.add(path)
        for item in deletions:
            if not isinstance(item, dict):
                raise RepoPublicationError("github_publish_payload_invalid")
            path = str(item.get("path") or "")
            parsed = PurePosixPath(path)
            if (
                not path
                or parsed.is_absolute()
                or ".." in parsed.parts
                or path.startswith(".chronos/")
                or path in seen
            ):
                raise RepoPublicationError("github_publish_payload_invalid")
            seen.add(path)

    async def _save_publication_state(
        self,
        workspace: dict[str, Any],
        key_hash: str,
        state: dict[str, Any],
    ) -> dict[str, Any]:
        metadata = dict(workspace.get("metadata") or {})
        publications = dict(metadata.get("pr_publications") or {})
        publications[key_hash] = {**state, "updated_at": _iso()}
        if len(publications) > 25:
            ordered = sorted(
                publications.items(), key=lambda item: str(item[1].get("updated_at") or "")
            )
            publications = dict(ordered[-25:])
        metadata["pr_publications"] = publications
        return await self._update_workspace(workspace["id"], metadata=metadata)

    @staticmethod
    def _provider_evidence(state: dict[str, Any]) -> dict[str, Any] | None:
        if state.get("stage") != "pr_created":
            return None
        evidence = {
            "id": state.get("provider_pr_id"),
            "node_id": state.get("provider_node_id"),
            "number": state.get("provider_number"),
            "commit_oid": state.get("provider_commit_oid"),
            "url": state.get("provider_url"),
        }
        if (
            not isinstance(evidence["id"], int)
            or not isinstance(evidence["number"], int)
            or not str(evidence["node_id"] or "")
            or re.fullmatch(r"[0-9a-f]{40}", str(evidence["commit_oid"] or "")) is None
            or not str(evidence["url"] or "").startswith("https://github.com/")
        ):
            raise RepoPublicationError("github_provider_unavailable")
        return evidence


production_repo_workspace_connector = ProductionRepoWorkspaceConnector()
