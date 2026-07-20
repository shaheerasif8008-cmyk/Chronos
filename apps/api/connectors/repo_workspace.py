from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import shlex
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.execution_boundary import blocks_api_host_tool, unavailable_host_execution_result
from core.models import ToolResult
from core.workspace import jailed_path, task_workspace_root_from_args

MAX_FILE_BYTES = 256_000
MAX_OUTPUT_BYTES = 128_000
DEFAULT_TIMEOUT_SECONDS = 20
FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "repo_workspace"
SHELL_METACHARS = {";", "&", "|", "`", "$", "<", ">"}


def _repo_root(args: dict[str, Any]) -> Path:
    root = task_workspace_root_from_args(args)
    repo_path = str(args.get("repo_path") or "repos/python_bug")
    return jailed_path(root, repo_path)


def _file_path(repo: Path, rel: str) -> Path:
    path = jailed_path(repo, rel)
    if repo != path and repo not in path.parents:
        raise ValueError("Path escapes the repo workspace")
    return path


def _chronos_path(repo: Path, name: str) -> Path:
    path = _file_path(repo, f".chronos/{name}")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate_branch_name(branch: str) -> str:
    branch = branch.strip()
    if not branch or branch.startswith("-") or ".." in branch or any(ch.isspace() for ch in branch):
        raise ValueError("Invalid branch name")
    return branch


def _validate_github_url(url: str) -> str:
    url = url.strip()
    if not re.fullmatch(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?", url):
        raise ValueError("Only public https://github.com/<owner>/<repo> clone URLs are supported")
    return url


def _safe_pytest_command(args: dict[str, Any]) -> list[str]:
    raw = str(args.get("command") or "").strip()
    if not raw:
        return ["pytest", "-q"]
    if any(ch in raw for ch in SHELL_METACHARS):
        raise ValueError("Only pytest commands without shell operators are allowed")
    tokens = shlex.split(raw)
    if not tokens or tokens[0] != "pytest":
        raise ValueError("Only pytest commands are allowed")
    safe: list[str] = ["pytest"]
    for token in tokens[1:]:
        if any(ch in token for ch in SHELL_METACHARS):
            raise ValueError("Only pytest commands without shell operators are allowed")
        if token.startswith("/"):
            raise ValueError("Absolute test paths are not allowed")
        if token.startswith("../") or token == ".." or "/../" in token:
            raise ValueError("Path escapes the repo workspace")
        safe.append(token)
    return safe


async def _run(
    argv: list[str],
    *,
    cwd: Path,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    extra_env: dict[str, str] | None = None,
) -> tuple[int, bytes, bytes, bool]:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONDONTWRITEBYTECODE": "1",
        "HOME": str(cwd),
        "GIT_TERMINAL_PROMPT": "0",
        "GCM_INTERACTIVE": "Never",
        "GIT_ASKPASS": "/bin/false",
        "SSH_ASKPASS": "/bin/false",
    }
    env.update(extra_env or {})
    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd),
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
        return int(process.returncode or 0), stdout, stderr, False
    except asyncio.TimeoutError:
        process.kill()
        stdout, stderr = await process.communicate()
        return int(process.returncode or -1), stdout, stderr, True


def _decode(raw: bytes) -> tuple[str, bool]:
    truncated = len(raw) > MAX_OUTPUT_BYTES
    return raw[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace"), truncated


class RepoWorkspaceConnector:
    async def execute(self, tool: str, args: dict[str, Any]) -> ToolResult:
        # Defense in depth: the ToolBroker applies the same gate before routing,
        # but connector-level callers must not be able to bypass it. Every repo
        # action is blocked, including read/write-only actions, because a host
        # working tree would let a later tool execute cloned code with the API
        # container's task-role credentials.
        if blocks_api_host_tool(tool):
            return unavailable_host_execution_result(tool)
        args.pop("__connector_tier", None)
        if tool == "repo.clone":
            return await self._clone(args)
        if tool == "repo.open_fixture":
            return await self._open_fixture(args)
        if tool == "repo.create_branch":
            return await self._create_branch(args)
        if tool == "repo.list_files":
            return await self._list_files(args)
        if tool == "repo.read_file":
            return await self._read_file(args)
        if tool == "repo.write_file":
            return await self._write_file(args)
        if tool == "repo.run_tests":
            return await self._run_tests(args)
        if tool == "repo.diff":
            return await self._diff(args)
        if tool == "repo.status":
            return await self._status(args)
        if tool == "repo.commit":
            return await self._commit(args)
        if tool == "repo.create_pr":
            return await self._create_pr(args)
        if tool == "repo.review":
            return await self._review(args)
        raise ValueError(f"Unknown repo tool: {tool}")

    async def _init_repo(self, repo: Path, *, message: str) -> str:
        await _run(["git", "init", "-b", "main"], cwd=repo)
        await _run(["git", "config", "user.email", "chronos@example.local"], cwd=repo)
        await _run(["git", "config", "user.name", "Chronos"], cwd=repo)
        await _run(["git", "add", "."], cwd=repo)
        returncode, stdout_raw, stderr_raw, timed_out = await _run(["git", "commit", "-m", message], cwd=repo)
        if returncode != 0 or timed_out:
            err, _ = _decode(stderr_raw + stdout_raw)
            raise RuntimeError(f"git commit failed: {err}")
        returncode, stdout_raw, stderr_raw, timed_out = await _run(["git", "rev-parse", "HEAD"], cwd=repo)
        if returncode != 0 or timed_out:
            err, _ = _decode(stderr_raw)
            raise RuntimeError(f"git rev-parse failed: {err}")
        sha, _ = _decode(stdout_raw)
        return sha.strip()

    async def _clone(self, args: dict[str, Any]) -> ToolResult:
        root = task_workspace_root_from_args(args)
        repo = jailed_path(root, str(args.get("repo_path") or "repos/imported"))
        if repo.exists():
            shutil.rmtree(repo)
        source_path = str(args.get("source_path") or "").strip()
        source_url = str(args.get("source_url") or "").strip()
        source_meta: dict[str, Any]
        if source_path:
            import core.workspace as workspace

            workspace_root = workspace.WORKSPACE_ROOT.resolve()
            source = Path(source_path).expanduser().resolve()
            if workspace_root != source and workspace_root not in source.parents:
                raise ValueError("Path escapes the task workspace")
            if not source.is_dir():
                raise FileNotFoundError(source_path)
            shutil.copytree(source, repo, ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache"))
            source_meta = {"type": "local_path", "path": str(source)}
            sha = await self._init_repo(repo, message="Import local repository")
        elif source_url:
            url = _validate_github_url(source_url)
            repo.parent.mkdir(parents=True, exist_ok=True)
            returncode, stdout_raw, stderr_raw, timed_out = await _run(
                ["git", "clone", "--depth", "1", url, str(repo)],
                cwd=root,
                timeout_seconds=min(int(args.get("timeout_seconds") or 30), 60),
            )
            if returncode != 0 or timed_out:
                err, _ = _decode(stderr_raw + stdout_raw)
                raise RuntimeError(f"git clone failed: {err}")
            await _run(["git", "config", "user.email", "chronos@example.local"], cwd=repo)
            await _run(["git", "config", "user.name", "Chronos"], cwd=repo)
            source_meta = {"type": "github", "url": url}
            returncode, stdout_raw, stderr_raw, timed_out = await _run(["git", "rev-parse", "HEAD"], cwd=repo)
            if returncode != 0 or timed_out:
                err, _ = _decode(stderr_raw)
                raise RuntimeError(f"git rev-parse failed: {err}")
            sha, _ = _decode(stdout_raw)
            sha = sha.strip()
        else:
            raise ValueError("repo.clone requires source_path or source_url")
        files = self._repo_files(repo)
        return ToolResult(
            data={"repo_path": str(repo.relative_to(root)), "branch": "main", "sha": sha, "source": source_meta, "files": files},
            summary=f"Imported repo into {repo.relative_to(root)}",
        )

    async def _open_fixture(self, args: dict[str, Any]) -> ToolResult:
        root = task_workspace_root_from_args(args)
        name = str(args.get("name") or "python_bug")
        if "/" in name or "\\" in name or name in {"", ".", ".."}:
            raise ValueError("Invalid fixture name")
        source = (FIXTURE_ROOT / name).resolve()
        if FIXTURE_ROOT.resolve() not in source.parents or not source.is_dir():
            raise FileNotFoundError(f"fixture repo {name}")
        repo = jailed_path(root, str(args.get("repo_path") or f"repos/{name}"))
        if repo.exists():
            shutil.rmtree(repo)
        shutil.copytree(source, repo)
        await self._init_repo(repo, message="Import fixture repo")
        files = self._repo_files(repo)
        return ToolResult(
            data={"repo_path": str(repo.relative_to(root)), "branch": "main", "files": files},
            summary=f"Opened fixture repo {name}",
        )

    def _repo_files(self, repo: Path) -> list[str]:
        return [
            str(path.relative_to(repo))
            for path in sorted(repo.rglob("*"))
            if path.is_file() and ".git" not in path.parts and ".chronos" not in path.parts and "__pycache__" not in path.parts
        ]

    async def _create_branch(self, args: dict[str, Any]) -> ToolResult:
        repo = _repo_root(args)
        branch = _validate_branch_name(str(args.get("branch") or ""))
        returncode, stdout, stderr, timed_out = await _run(["git", "checkout", "-B", branch], cwd=repo)
        if returncode != 0 or timed_out:
            err, _ = _decode(stderr)
            raise RuntimeError(f"git checkout failed: {err}")
        return ToolResult(data={"branch": branch}, summary=f"Created branch {branch}")

    async def _list_files(self, args: dict[str, Any]) -> ToolResult:
        repo = _repo_root(args)
        files = self._repo_files(repo)
        return ToolResult(data={"files": files, "count": len(files)}, summary=f"Listed {len(files)} repo files")

    async def _read_file(self, args: dict[str, Any]) -> ToolResult:
        repo = _repo_root(args)
        path = _file_path(repo, str(args.get("path") or ""))
        if not path.is_file():
            raise FileNotFoundError(str(args.get("path") or ""))
        raw = path.read_bytes()
        truncated = len(raw) > MAX_FILE_BYTES
        content = raw[:MAX_FILE_BYTES].decode("utf-8", errors="replace")
        return ToolResult(
            data={"path": str(path.relative_to(repo)), "content": content, "truncated": truncated, "bytes": len(raw)},
            summary=f"Read {path.relative_to(repo)}",
        )

    async def _write_file(self, args: dict[str, Any]) -> ToolResult:
        repo = _repo_root(args)
        path = _file_path(repo, str(args.get("path") or ""))
        content = str(args.get("content") or "")
        raw = content.encode("utf-8")
        if len(raw) > MAX_FILE_BYTES:
            raise ValueError(f"repo.write_file payload exceeds {MAX_FILE_BYTES} bytes")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        return ToolResult(
            data={"path": str(path.relative_to(repo)), "bytes": len(raw)},
            summary=f"Wrote {path.relative_to(repo)}",
        )

    async def _run_tests(self, args: dict[str, Any]) -> ToolResult:
        repo = _repo_root(args)
        timeout = min(int(args.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS), 30)
        pytest_bin = shutil.which("pytest")
        command = _safe_pytest_command(args)
        extra_env: dict[str, str] = {}
        if pytest_bin:
            argv = [pytest_bin, *command[1:]]
        else:
            try:
                import pytest  # type: ignore
            except ImportError as exc:
                raise RuntimeError("pytest executable is not available") from exc
            pytest_site = str(Path(pytest.__file__).resolve().parents[1])
            pythonpath = os.environ.get("PYTHONPATH", "")
            extra_env["PYTHONPATH"] = pytest_site if not pythonpath else f"{pytest_site}{os.pathsep}{pythonpath}"
            argv = [sys.executable, "-m", "pytest", *command[1:]]
        returncode, stdout_raw, stderr_raw, timed_out = await _run(argv, cwd=repo, timeout_seconds=timeout, extra_env=extra_env)
        stdout, stdout_truncated = _decode(stdout_raw)
        stderr, stderr_truncated = _decode(stderr_raw)
        status = "timeout" if timed_out else ("success" if returncode == 0 else "failure")
        return ToolResult(
            data={
                "status": status,
                "returncode": returncode,
                "stdout": stdout,
                "stderr": stderr,
                "stdout_truncated": stdout_truncated,
                "stderr_truncated": stderr_truncated,
                "command": command,
            },
            summary=f"Repo tests {status}",
        )

    async def _diff(self, args: dict[str, Any]) -> ToolResult:
        repo = _repo_root(args)
        returncode, stdout_raw, stderr_raw, timed_out = await _run(["git", "diff", "--", "."], cwd=repo)
        if returncode != 0 or timed_out:
            err, _ = _decode(stderr_raw)
            raise RuntimeError(f"git diff failed: {err}")
        diff, truncated = _decode(stdout_raw)
        return ToolResult(data={"diff": diff, "truncated": truncated}, summary="Generated repo diff")

    async def _status(self, args: dict[str, Any]) -> ToolResult:
        repo = _repo_root(args)
        returncode, branch_raw, stderr_raw, timed_out = await _run(["git", "branch", "--show-current"], cwd=repo)
        if returncode != 0 or timed_out:
            err, _ = _decode(stderr_raw)
            raise RuntimeError(f"git branch failed: {err}")
        branch, _ = _decode(branch_raw)
        returncode, status_raw, stderr_raw, timed_out = await _run(["git", "status", "--porcelain=v1"], cwd=repo)
        if returncode != 0 or timed_out:
            err, _ = _decode(stderr_raw)
            raise RuntimeError(f"git status failed: {err}")
        status, _ = _decode(status_raw)
        changes = []
        for line in status.splitlines():
            if not line:
                continue
            path = line[3:] if len(line) > 3 else ""
            changes.append({"status": line[:2].strip(), "path": path})
        return ToolResult(
            data={"branch": branch.strip() or "main", "dirty": bool(changes), "changes": changes},
            summary=f"Repo status: {'dirty' if changes else 'clean'}",
        )

    async def _commit(self, args: dict[str, Any]) -> ToolResult:
        repo = _repo_root(args)
        message = str(args.get("message") or "").strip()
        if not message:
            raise ValueError("Commit message is required")
        await _run(["git", "add", "."], cwd=repo)
        returncode, stdout_raw, stderr_raw, timed_out = await _run(["git", "commit", "-m", message], cwd=repo)
        if returncode != 0 or timed_out:
            err, _ = _decode(stderr_raw + stdout_raw)
            raise RuntimeError(f"git commit failed: {err}")
        returncode, sha_raw, stderr_raw, timed_out = await _run(["git", "rev-parse", "HEAD"], cwd=repo)
        if returncode != 0 or timed_out:
            err, _ = _decode(stderr_raw)
            raise RuntimeError(f"git rev-parse failed: {err}")
        sha, _ = _decode(sha_raw)
        return ToolResult(data={"sha": sha.strip(), "message": message}, summary=f"Committed {sha.strip()[:12]}")

    async def _create_pr(self, args: dict[str, Any]) -> ToolResult:
        repo = _repo_root(args)
        title = str(args.get("title") or "").strip()
        body = str(args.get("body") or "").strip()
        base = _validate_branch_name(str(args.get("base") or "main"))
        head = _validate_branch_name(str(args.get("head") or ""))
        approval_id = str(
            args.get("__approval_id")
            if args.get("__approved_by_gate")
            else args.get("approval_id")
            or ""
        ).strip()
        if not title:
            raise ValueError("PR title is required")
        if not approval_id:
            return ToolResult(
                data={
                    "status": "approval_required",
                    "risk_level": "repo_pull_request",
                    "required_approval": {"action": "repo.create_pr", "title": title, "base": base, "head": head or "current"},
                },
                summary="PR creation requires approval",
            )
        if not head:
            returncode, branch_raw, stderr_raw, timed_out = await _run(["git", "branch", "--show-current"], cwd=repo)
            if returncode != 0 or timed_out:
                err, _ = _decode(stderr_raw)
                raise RuntimeError(f"git branch failed: {err}")
            branch, _ = _decode(branch_raw)
            head = branch.strip() or "main"
        returncode, sha_raw, stderr_raw, timed_out = await _run(["git", "rev-parse", "HEAD"], cwd=repo)
        if returncode != 0 or timed_out:
            err, _ = _decode(stderr_raw)
            raise RuntimeError(f"git rev-parse failed: {err}")
        sha, _ = _decode(sha_raw)
        pr_id = uuid.uuid4().hex
        payload = {
            "id": pr_id,
            "status": "ready",
            "title": title,
            "body": body,
            "base": base,
            "head": head,
            "head_sha": sha.strip(),
            "approval_id": approval_id,
            "url": f"chronos://repo-pr/{pr_id}",
            "created_at": _now(),
            "publication": "recorded_local_pr_request",
        }
        artifact = _chronos_path(repo, "pull_request.json")
        artifact.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        payload["artifact_path"] = str(artifact.relative_to(repo))
        return ToolResult(data=payload, summary=f"Recorded PR request {title}")

    async def _review(self, args: dict[str, Any]) -> ToolResult:
        repo = _repo_root(args)
        title = str(args.get("title") or "Code review").strip()
        returncode, diff_raw, stderr_raw, timed_out = await _run(["git", "diff", "--", "."], cwd=repo)
        if returncode != 0 or timed_out:
            err, _ = _decode(stderr_raw)
            raise RuntimeError(f"git diff failed: {err}")
        diff, _ = _decode(diff_raw)
        changed_files = []
        for line in diff.splitlines():
            if line.startswith("+++ b/"):
                changed_files.append(line.removeprefix("+++ b/"))
        findings: list[dict[str, Any]] = []
        for rel in self._repo_files(repo):
            path = _file_path(repo, rel)
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except UnicodeDecodeError:
                continue
            for index, line in enumerate(lines, start=1):
                lowered = line.lower()
                if "todo" in lowered or "fixme" in lowered:
                    findings.append(
                        {
                            "file": rel,
                            "line": index,
                            "severity": "medium",
                            "title": "Unresolved marker",
                            "body": "Remove or resolve TODO/FIXME markers before opening the PR.",
                            "suggested_patch": line.replace("TODO", "Resolved").replace("FIXME", "Resolved"),
                        }
                    )
        if not findings and changed_files:
            findings.append(
                {
                    "file": changed_files[0],
                    "line": 1,
                    "severity": "info",
                    "title": "Review completed",
                    "body": "Changed file reviewed; no deterministic issue was found by the local reviewer.",
                    "suggested_patch": "",
                }
            )
        payload = {
            "title": title,
            "summary": {"changed_files": len(set(changed_files)), "finding_count": len(findings)},
            "findings": findings,
            "created_at": _now(),
        }
        artifact = _chronos_path(repo, "code_review.json")
        artifact.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        payload["artifact_path"] = str(artifact.relative_to(repo))
        return ToolResult(data=payload, summary=f"Code review produced {len(findings)} findings")


class RoutedRepoWorkspaceConnector:
    """Keep the ergonomic host implementation local and E2B-only in production."""

    def __init__(self) -> None:
        self.local = RepoWorkspaceConnector()

    async def execute(self, tool: str, args: dict[str, Any]) -> ToolResult:
        from core.config import settings

        if settings.is_production:
            from connectors.repo_workspace_remote import production_repo_workspace_connector

            return await production_repo_workspace_connector.execute(tool, args)
        return await self.local.execute(tool, args)


repo_workspace_connector = RoutedRepoWorkspaceConnector()
