from __future__ import annotations

import asyncio
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from core.models import ToolResult
from core.workspace import jailed_path, task_workspace_root_from_args

MAX_FILE_BYTES = 256_000
MAX_OUTPUT_BYTES = 128_000
DEFAULT_TIMEOUT_SECONDS = 20
FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "repo_workspace"


def _repo_root(args: dict[str, Any]) -> Path:
    root = task_workspace_root_from_args(args)
    repo_path = str(args.get("repo_path") or "repos/python_bug")
    return jailed_path(root, repo_path)


def _file_path(repo: Path, rel: str) -> Path:
    path = jailed_path(repo, rel)
    if repo != path and repo not in path.parents:
        raise ValueError("Path escapes the repo workspace")
    return path


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
        args.pop("__connector_tier", None)
        if tool == "repo.open_fixture":
            return await self._open_fixture(args)
        if tool == "repo.create_branch":
            return await self._create_branch(args)
        if tool == "repo.read_file":
            return await self._read_file(args)
        if tool == "repo.write_file":
            return await self._write_file(args)
        if tool == "repo.run_tests":
            return await self._run_tests(args)
        if tool == "repo.diff":
            return await self._diff(args)
        raise ValueError(f"Unknown repo tool: {tool}")

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
        await _run(["git", "init", "-b", "main"], cwd=repo)
        await _run(["git", "config", "user.email", "chronos@example.local"], cwd=repo)
        await _run(["git", "config", "user.name", "Chronos"], cwd=repo)
        await _run(["git", "add", "."], cwd=repo)
        await _run(["git", "commit", "-m", "Import fixture repo"], cwd=repo)
        files = [
            str(path.relative_to(repo))
            for path in sorted(repo.rglob("*"))
            if path.is_file() and ".git" not in path.parts
        ]
        return ToolResult(
            data={"repo_path": str(repo.relative_to(root)), "branch": "main", "files": files},
            summary=f"Opened fixture repo {name}",
        )

    async def _create_branch(self, args: dict[str, Any]) -> ToolResult:
        repo = _repo_root(args)
        branch = str(args.get("branch") or "").strip()
        if not branch or branch.startswith("-") or ".." in branch or any(ch.isspace() for ch in branch):
            raise ValueError("Invalid branch name")
        returncode, stdout, stderr, timed_out = await _run(["git", "checkout", "-B", branch], cwd=repo)
        if returncode != 0 or timed_out:
            err, _ = _decode(stderr)
            raise RuntimeError(f"git checkout failed: {err}")
        return ToolResult(data={"branch": branch}, summary=f"Created branch {branch}")

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
        extra_env: dict[str, str] = {}
        if pytest_bin:
            argv = [pytest_bin, "-q"]
        else:
            try:
                import pytest  # type: ignore
            except ImportError as exc:
                raise RuntimeError("pytest executable is not available") from exc
            pytest_site = str(Path(pytest.__file__).resolve().parents[1])
            pythonpath = os.environ.get("PYTHONPATH", "")
            extra_env["PYTHONPATH"] = pytest_site if not pythonpath else f"{pytest_site}{os.pathsep}{pythonpath}"
            argv = [sys.executable, "-m", "pytest", "-q"]
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


repo_workspace_connector = RepoWorkspaceConnector()
