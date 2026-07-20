"""Trusted repo-workspace command runner uploaded into the isolated sandbox.

This module is not imported by the API process.  ``RepoWorkspaceConnector``
uploads these bytes to E2B and invokes it with fixed, Chronos-generated control
file paths.  Model/user values travel only in JSON and are passed to
``subprocess`` as argv, never interpolated into a shell command.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Any


ROOT = Path("/home/user/workspace").resolve()
MAX_OUTPUT_BYTES = 128_000
MAX_FILE_BYTES = 256_000
MAX_PUBLISH_FILE_BYTES = 1_048_576
MAX_PUBLISH_TOTAL_BYTES = 10_485_760
MAX_PUBLISH_FILES = 200


def _path(value: str, *, root: Path | None = None) -> Path:
    root = (root or ROOT).resolve()
    raw = str(value or ".").strip().replace("\\", "/")
    if raw.startswith("/"):
        raise ValueError("absolute paths are forbidden")
    candidate = (root / raw).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError("path escapes workspace")
    return candidate


def _repo(spec: dict[str, Any]) -> Path:
    repo = _path(str(spec.get("repo_path") or "repos/imported"))
    if repo == ROOT:
        raise ValueError("repository path cannot be workspace root")
    return repo


def _repo_file(repo: Path, value: str) -> Path:
    raw = str(value or "").strip().replace("\\", "/")
    if not raw or raw.startswith("/"):
        raise ValueError("file path must be relative")
    path = (repo / raw).resolve()
    if path == repo or repo not in path.parents:
        raise ValueError("path escapes repository")
    return path


def _env(repo: Path) -> dict[str, str]:
    return {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(repo),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GCM_INTERACTIVE": "Never",
        "GIT_ASKPASS": "/bin/false",
        "SSH_ASKPASS": "/bin/false",
    }


def _run(argv: list[str], *, cwd: Path, timeout: int) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            argv,
            cwd=cwd,
            env=_env(cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=max(1, timeout),
            check=False,
        )
        timed_out = False
        stdout = completed.stdout
        stderr = completed.stderr
        returncode = int(completed.returncode)
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        stdout = bytes(exc.stdout or b"")
        stderr = bytes(exc.stderr or b"")
        returncode = -1
    return {
        "status": "timeout" if timed_out else ("success" if returncode == 0 else "failure"),
        "returncode": returncode,
        "stdout": stdout[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace"),
        "stderr": stderr[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace"),
        "stdout_truncated": len(stdout) > MAX_OUTPUT_BYTES,
        "stderr_truncated": len(stderr) > MAX_OUTPUT_BYTES,
    }


def _git(repo: Path, argv: list[str], *, timeout: int = 30) -> dict[str, Any]:
    return _run(["git", *argv], cwd=repo, timeout=timeout)


def _require_success(result: dict[str, Any], operation: str) -> None:
    if result["status"] != "success":
        raise RuntimeError(f"{operation}_failed")


def _init_repo(repo: Path, message: str) -> dict[str, Any]:
    _require_success(_git(repo, ["init", "-b", "main"]), "git_init")
    _require_success(_git(repo, ["config", "user.email", "chronos@cognisiatech.com"]), "git_config")
    _require_success(_git(repo, ["config", "user.name", "Chronos"]), "git_config")
    _require_success(_git(repo, ["add", "."]), "git_add")
    _require_success(_git(repo, ["commit", "-m", message]), "git_commit")
    sha = _git(repo, ["rev-parse", "HEAD"])
    _require_success(sha, "git_rev_parse")
    return {"branch": "main", "sha": sha["stdout"].strip()}


def _files(repo: Path) -> list[str]:
    files: list[str] = []
    for path in sorted(repo.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        rel = path.relative_to(repo)
        if any(part in {".git", ".chronos", "__pycache__", ".pytest_cache"} for part in rel.parts):
            continue
        files.append(str(rel))
    return files


def _safe_extract(archive: Path, destination: Path, *, strip_first: bool) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile() and not member.isdir():
                continue
            parts = Path(member.name).parts
            if strip_first:
                parts = parts[1:]
            if not parts:
                continue
            target = _repo_file(destination, str(Path(*parts)))
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source = tar.extractfile(member)
            if source is None:
                continue
            with target.open("wb") as handle:
                shutil.copyfileobj(source, handle)


def _archive(repo: Path, archive: Path) -> int:
    archive.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "w:gz") as tar:
        for path in sorted(repo.rglob("*")):
            if path.is_symlink():
                continue
            rel = path.relative_to(repo)
            if any(part in {"__pycache__", ".pytest_cache"} for part in rel.parts):
                continue
            tar.add(path, arcname=str(rel), recursive=False)
    return archive.stat().st_size


def execute(spec: dict[str, Any]) -> dict[str, Any]:
    action = str(spec.get("action") or "")
    repo = _repo(spec)

    if action == "clone":
        if repo.exists():
            shutil.rmtree(repo)
        repo.parent.mkdir(parents=True, exist_ok=True)
        result = _run(
            ["git", "clone", "--depth", "1", str(spec["source_url"]), str(repo)],
            cwd=repo.parent,
            timeout=int(spec.get("timeout_seconds") or 60),
        )
        _require_success(result, "git_clone")
        requested_ref = str(spec.get("ref") or "HEAD")
        if requested_ref != "HEAD":
            _require_success(
                _git(repo, ["fetch", "--depth", "1", "origin", requested_ref]),
                "git_fetch_ref",
            )
            _require_success(
                _git(repo, ["checkout", "--detach", "FETCH_HEAD"]),
                "git_checkout_ref",
            )
        _require_success(_git(repo, ["config", "user.email", "chronos@cognisiatech.com"]), "git_config")
        _require_success(_git(repo, ["config", "user.name", "Chronos"]), "git_config")
        sha = _git(repo, ["rev-parse", "HEAD"])
        branch = _git(repo, ["branch", "--show-current"])
        _require_success(sha, "git_rev_parse")
        _require_success(branch, "git_branch")
        return {
            "sha": sha["stdout"].strip(),
            "branch": (
                requested_ref
                if requested_ref != "HEAD"
                else branch["stdout"].strip() or "main"
            ),
            "files": _files(repo),
        }

    if action == "extract_archive":
        if repo.exists():
            shutil.rmtree(repo)
        repo.mkdir(parents=True, exist_ok=True)
        archive = _path(str(spec["archive_path"]))
        _safe_extract(archive, repo, strip_first=bool(spec.get("strip_first", True)))
        return {**_init_repo(repo, str(spec.get("message") or "Import repository snapshot")), "files": _files(repo)}

    if action == "init":
        repo.mkdir(parents=True, exist_ok=True)
        return {**_init_repo(repo, str(spec.get("message") or "Initialize repository")), "files": _files(repo)}

    if action == "write_uninitialized":
        import base64

        repo.mkdir(parents=True, exist_ok=True)
        path = _repo_file(repo, str(spec["path"]))
        raw = base64.b64decode(str(spec["content_b64"]), validate=True)
        if len(raw) > MAX_FILE_BYTES:
            raise ValueError("file payload exceeds limit")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        return {"path": str(path.relative_to(repo)), "bytes": len(raw)}

    if not repo.is_dir() or not (repo / ".git").is_dir():
        raise FileNotFoundError("repository_not_initialized")

    if action == "create_branch":
        result = _git(repo, ["checkout", "-B", str(spec["branch"])])
        _require_success(result, "git_checkout")
        return {"branch": str(spec["branch"])}
    if action == "list_files":
        files = _files(repo)
        return {"files": files, "count": len(files)}
    if action == "read_file":
        path = _repo_file(repo, str(spec["path"]))
        if path.is_symlink() or not path.is_file():
            raise FileNotFoundError("repo_file_not_found")
        raw = path.read_bytes()
        return {
            "path": str(path.relative_to(repo)),
            "content": raw[:MAX_FILE_BYTES].decode("utf-8", errors="replace"),
            "truncated": len(raw) > MAX_FILE_BYTES,
            "bytes": len(raw),
        }
    if action == "write_file":
        import base64

        path = _repo_file(repo, str(spec["path"]))
        if path.exists() and path.is_symlink():
            raise ValueError("symlink writes are forbidden")
        raw = base64.b64decode(str(spec["content_b64"]), validate=True)
        if len(raw) > MAX_FILE_BYTES:
            raise ValueError("file payload exceeds limit")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        return {"path": str(path.relative_to(repo)), "bytes": len(raw)}
    if action == "run_tests":
        command = [str(token) for token in spec["command"]]
        return {**_run(command, cwd=repo, timeout=int(spec.get("timeout_seconds") or 30)), "command": command}
    if action == "diff":
        result = _git(repo, ["diff", "--", "."])
        _require_success(result, "git_diff")
        return {"diff": result["stdout"], "truncated": result["stdout_truncated"]}
    if action == "status":
        branch = _git(repo, ["branch", "--show-current"])
        status = _git(repo, ["status", "--porcelain=v1"])
        _require_success(branch, "git_branch")
        _require_success(status, "git_status")
        changes = [
            {"status": line[:2].strip(), "path": line[3:] if len(line) > 3 else ""}
            for line in status["stdout"].splitlines()
            if line
        ]
        return {"branch": branch["stdout"].strip() or "main", "dirty": bool(changes), "changes": changes}
    if action == "commit":
        _require_success(_git(repo, ["add", "."]), "git_add")
        result = _git(repo, ["commit", "-m", str(spec["message"])])
        _require_success(result, "git_commit")
        sha = _git(repo, ["rev-parse", "HEAD"])
        _require_success(sha, "git_rev_parse")
        return {"sha": sha["stdout"].strip(), "message": str(spec["message"])}
    if action == "publication_changes":
        import base64

        base_sha = str(spec["base_sha"])
        if re.fullmatch(r"[0-9a-f]{40}", base_sha) is None:
            raise ValueError("invalid base sha")
        branch = _git(repo, ["branch", "--show-current"])
        head_sha = _git(repo, ["rev-parse", "HEAD"])
        status = _git(repo, ["status", "--porcelain=v1"])
        changes = _git(
            repo,
            ["diff", "--name-status", "-z", "--find-renames", f"{base_sha}..HEAD"],
        )
        for result, operation in (
            (branch, "git_branch"),
            (head_sha, "git_rev_parse"),
            (status, "git_status"),
            (changes, "git_diff_names"),
        ):
            _require_success(result, operation)
        if changes["stdout_truncated"]:
            raise ValueError("publication change list exceeds limit")
        tokens = changes["stdout"].split("\0")
        if tokens and tokens[-1] == "":
            tokens.pop()
        additions: list[dict[str, str]] = []
        deletions: list[dict[str, str]] = []
        total_bytes = 0
        index = 0
        while index < len(tokens):
            change_type = tokens[index]
            index += 1
            if index >= len(tokens):
                raise ValueError("malformed git change list")
            first_path = tokens[index]
            index += 1
            if change_type.startswith(("R", "C")):
                if index >= len(tokens):
                    raise ValueError("malformed git rename list")
                path = tokens[index]
                index += 1
                if change_type.startswith("R") and not first_path.startswith(".chronos/"):
                    deletions.append({"path": first_path})
            else:
                path = first_path
            if path.startswith(".chronos/"):
                continue
            if change_type.startswith("D"):
                deletions.append({"path": path})
                continue
            file_path = _repo_file(repo, path)
            if file_path.is_symlink() or not file_path.is_file():
                raise ValueError("publication contains unsupported file type")
            raw = file_path.read_bytes()
            if len(raw) > MAX_PUBLISH_FILE_BYTES:
                raise ValueError("publication file exceeds limit")
            total_bytes += len(raw)
            if total_bytes > MAX_PUBLISH_TOTAL_BYTES:
                raise ValueError("publication payload exceeds limit")
            additions.append(
                {"path": path, "contents": base64.b64encode(raw).decode("ascii")}
            )
            if len(additions) + len(deletions) > MAX_PUBLISH_FILES:
                raise ValueError("publication file count exceeds limit")
        dirty_lines = []
        for line in status["stdout"].splitlines():
            path = line[3:] if len(line) > 3 else ""
            if path.startswith(".chronos/") or path == ".chronos":
                continue
            dirty_lines.append(line)
        return {
            "branch": branch["stdout"].strip(),
            "head_sha": head_sha["stdout"].strip(),
            "dirty": bool(dirty_lines),
            "additions": additions,
            "deletions": deletions,
            "file_count": len(additions) + len(deletions),
            "total_bytes": total_bytes,
        }
    if action == "create_pr":
        branch = _git(repo, ["branch", "--show-current"])
        sha = _git(repo, ["rev-parse", "HEAD"])
        _require_success(branch, "git_branch")
        _require_success(sha, "git_rev_parse")
        artifact = _repo_file(repo, ".chronos/pull_request.json")
        artifact.parent.mkdir(parents=True, exist_ok=True)
        provider = dict(spec.get("provider") or {})
        payload = {
            "id": str(spec["request_id"]),
            "status": "published",
            "title": str(spec["title"]),
            "body": str(spec.get("body") or ""),
            "base": str(spec.get("base") or "main"),
            "head": str(spec.get("head") or branch["stdout"].strip() or "main"),
            "head_sha": str(provider["commit_oid"]),
            "approval_id": str(spec["approval_id"]),
            "url": str(provider["url"]),
            "created_at": str(spec["created_at"]),
            "publication": "github_pull_request_created",
            "provider": "github",
            "provider_pr_id": provider["id"],
            "provider_node_id": provider["node_id"],
            "provider_number": provider["number"],
            "provider_commit_oid": provider["commit_oid"],
        }
        artifact.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        return {
            "status": "published",
            "provider": "github",
            "provider_pr_id": provider["id"],
            "provider_node_id": provider["node_id"],
            "provider_number": provider["number"],
            "provider_commit_oid": provider["commit_oid"],
            "url": str(provider["url"]),
            "artifact_path": ".chronos/pull_request.json",
        }
    if action == "review":
        diff = _git(repo, ["diff", "--", "."])
        _require_success(diff, "git_diff")
        changed = [line.removeprefix("+++ b/") for line in diff["stdout"].splitlines() if line.startswith("+++ b/")]
        findings: list[dict[str, Any]] = []
        for rel in _files(repo):
            path = _repo_file(repo, rel)
            for index, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
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
        if not findings and changed:
            findings.append(
                {
                    "file": changed[0],
                    "line": 1,
                    "severity": "info",
                    "title": "Review completed",
                    "body": "Changed file reviewed; no deterministic issue was found by the local reviewer.",
                    "suggested_patch": "",
                }
            )
        payload = {
            "title": str(spec.get("title") or "Code review"),
            "summary": {"changed_files": len(set(changed)), "finding_count": len(findings)},
            "findings": findings,
            "created_at": str(spec["created_at"]),
        }
        artifact = _repo_file(repo, ".chronos/code_review.json")
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        return {**payload, "artifact_path": ".chronos/code_review.json"}
    if action == "archive":
        archive = _path(str(spec["archive_path"]))
        return {"bytes": _archive(repo, archive)}
    if action == "restore":
        archive = _path(str(spec["archive_path"]))
        if repo.exists():
            shutil.rmtree(repo)
        repo.mkdir(parents=True, exist_ok=True)
        _safe_extract(archive, repo, strip_first=False)
        if not (repo / ".git").is_dir():
            raise ValueError("snapshot missing git metadata")
        return {"restored": True}
    raise ValueError("unknown repo action")


def main() -> None:
    spec_path = Path(sys.argv[1])
    result_path = Path(sys.argv[2])
    try:
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        result = {"ok": True, "data": execute(spec)}
    except Exception as exc:  # noqa: BLE001 - isolated runner returns a stable code
        result = {"ok": False, "error_code": type(exc).__name__}
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(result, separators=(",", ":")), encoding="utf-8")


if __name__ == "__main__":
    main()
