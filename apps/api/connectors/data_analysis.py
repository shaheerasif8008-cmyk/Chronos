"""Data analysis connector — routes through tool_broker.execute only.

Executes Python analysis code (pandas/matplotlib/numpy) in a sandboxed subprocess
against a materialized dataset CSV, captures stdout + produced chart PNGs, and
persists them as artifacts. All connector logic lives here; no business logic
belongs in the router.

Sandbox rules:
- Subprocess runs with -I (isolated; loads venv site-packages but not user-site or PYTHONPATH).
- pandas, matplotlib, numpy are allowed.
- Network access (socket/requests/httpx/urllib) is blocked.
- subprocess/multiprocessing/ctypes are blocked.
- os.system/popen/exec are blocked.
- Absolute-path open('/...') is blocked.
- MPLBACKEND=Agg: no display dependency.
- MPLCONFIGDIR: writable workspace subdirectory.
- OPENBLAS_NUM_THREADS=1, OMP_NUM_THREADS=1: prevent import-time virtual memory exhaustion.
- RLIMIT_CPU = 15 s, RLIMIT_AS = 2 GB, RLIMIT_FSIZE = 25 MB.
- Wall-clock timeout = 20 s.

Dataset materialization is org-checked BEFORE any file is written.
"""
from __future__ import annotations

import asyncio
import os
import re
import resource
import shutil
import signal
import sys
import uuid
from pathlib import Path
from typing import Any

from core.models import ToolResult

WORKSPACE_ROOT = Path("/tmp/chronos_task_workspaces")
MAX_CODE_BYTES = 128_000
MAX_OUTPUT_BYTES = 128_000
DATA_TIMEOUT_SECONDS = 20
DATA_RLIMIT_CPU = 15
DATA_RLIMIT_AS = 2 * 1024 * 1024 * 1024   # 2 GB
DATA_RLIMIT_FSIZE = 25 * 1024 * 1024       # 25 MB
DATA_RLIMIT_NPROC = 64                      # cap forks (anti fork-bomb)
#: Hard cap on the source artifact bytes read into the API process (anti-OOM).
MAX_SOURCE_BYTES = 256 * 1024 * 1024        # 256 MB

# Forbidden patterns for data sandbox (extends code.py patterns but allows pandas/matplotlib/numpy).
_FORBIDDEN_DATA_PATTERNS = [
    # Block network
    r"\bimport\s+(socket|requests|httpx|urllib)\b",
    r"\bfrom\s+(socket|requests|httpx|urllib)\b",
    # Block subprocess/concurrency/ctypes (openblas threads are set via env, not code)
    r"\bimport\s+(subprocess|multiprocessing|ctypes|resource)\b",
    r"\bfrom\s+(subprocess|multiprocessing|ctypes|resource)\b",
    # Block dynamic import (including importlib.import_module / builtins bypasses)
    r"__import__\s*\(",
    r"\bimportlib\b",
    r"\bimport_module\s*\(",
    r"\bimport\s+builtins\b",
    r"\bfrom\s+builtins\b",
    # Block absolute-path open
    r"\bopen\s*\(\s*['\"]/",
    # Block shell execution and forking
    r"\bos\.(system|popen|spawn|exec|fork|kill|killpg)",
    # Block arbitrary code execution / introspection escapes
    r"\beval\s*\(",
    r"\bexec\s*\(",
    r"\bcompile\s*\(",
]


def _data_workspace(org_id: str, run_id: str) -> Path:
    """Return a fresh, writable workspace dir for this analysis run."""
    root = (WORKSPACE_ROOT / org_id / run_id).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _set_data_limits() -> None:
    """Set conservative resource limits for the data analysis subprocess."""
    limits = [
        (resource.RLIMIT_CPU, (DATA_RLIMIT_CPU, DATA_RLIMIT_CPU)),
        (resource.RLIMIT_AS, (DATA_RLIMIT_AS, DATA_RLIMIT_AS)),
        (resource.RLIMIT_FSIZE, (DATA_RLIMIT_FSIZE, DATA_RLIMIT_FSIZE)),
    ]
    # Cap process count to stop fork-bombs from exhausting the host process table.
    if hasattr(resource, "RLIMIT_NPROC"):
        limits.append((resource.RLIMIT_NPROC, (DATA_RLIMIT_NPROC, DATA_RLIMIT_NPROC)))
    for limit, value in limits:
        try:
            resource.setrlimit(limit, value)
        except (OSError, ValueError):
            continue


def _validate_data_code(code: str) -> None:
    """Raise ValueError when code contains a forbidden import or filesystem operation.

    Args:
        code: Analysis code string submitted by the caller.

    Raises:
        ValueError: When a forbidden pattern is found.
    """
    if len(code.encode("utf-8")) > MAX_CODE_BYTES:
        raise ValueError(f"data.run code payload exceeds {MAX_CODE_BYTES} bytes")
    for pattern in _FORBIDDEN_DATA_PATTERNS:
        if re.search(pattern, code):
            raise ValueError("data.run rejected an unsafe import or filesystem operation")


async def _materialize_dataset(dataset_id: str, org_id: str, workspace: Path) -> tuple[Path, str] | None:
    """Load the dataset source artifact and write it to the workspace as data.csv (or data.json).

    Performs org check before writing any file.

    Args:
        dataset_id: UUID of the datasets row.
        org_id: Caller's organization id (used to enforce the tenant boundary).
        workspace: Local path to write the materialized file into.

    Returns:
        Tuple of (local_path, filename) on success, or None when the dataset is not
        found / belongs to a different org.
    """
    from core.artifacts import get_artifact, read_artifact_content
    from core.db import engine, reflect_table

    # 1. Load the datasets row and verify ownership.
    datasets_tbl = await reflect_table("datasets")
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                datasets_tbl.select().where(datasets_tbl.c.id == dataset_id)
            )
        ).mappings().first()

    if row is None:
        return None

    if str(row["organization_id"]) != str(org_id):
        return None  # cross-org access denied

    # 2. Load the source artifact bytes.
    source_artifact_id = str(row["source_artifact_id"]) if row.get("source_artifact_id") else None
    if not source_artifact_id:
        return None

    artifact_meta = await get_artifact(source_artifact_id)
    if artifact_meta is None:
        return None

    # Org-check the artifact too.
    if str(artifact_meta.get("organization_id", "")) != str(org_id):
        return None

    # Reject oversized sources before reading them into the API process (anti-OOM).
    declared_size = int(artifact_meta.get("size_bytes") or 0)
    if declared_size > MAX_SOURCE_BYTES:
        raise ValueError(f"dataset source exceeds {MAX_SOURCE_BYTES} byte limit")

    content = await read_artifact_content(source_artifact_id)
    if not content:
        return None
    if len(content) > MAX_SOURCE_BYTES:
        raise ValueError(f"dataset source exceeds {MAX_SOURCE_BYTES} byte limit")

    # 3. Write to workspace with a stable name.
    mime = str(artifact_meta.get("mime_type", "") or "")
    if "json" in mime:
        filename = "data.json"
    else:
        filename = "data.csv"

    dest = workspace / filename
    dest.write_bytes(content)
    return dest, filename


class DataAnalysisConnector:
    """Connector for the ``data.run`` tool."""

    async def execute(self, tool: str, args: dict[str, Any]) -> ToolResult:
        """Execute a data analysis tool call.

        Args:
            tool: Must be "data.run".
            args: Tool arguments, including broker-injected keys
                ``__connector_tier``, ``__org_id``, ``__task_id``.

        Returns:
            ToolResult with artifact ids for generated charts/reports, plus
            stdout preview. Honest error ToolResult when sandbox fails or code
            is rejected.
        """
        args.pop("__connector_tier", None)
        org_id: str = str(args.pop("__org_id", "default") or "default")
        task_id: str | None = args.pop("__task_id", None)

        if tool != "data.run":
            raise ValueError(f"Unknown data tool: {tool}")

        return await self._run(args, org_id=org_id, task_id=task_id)

    async def _run(
        self, args: dict[str, Any], *, org_id: str, task_id: str | None
    ) -> ToolResult:
        """Handle ``data.run``.

        Args:
            args: Tool arguments (dataset_id, code).
            org_id: Tenant scope injected by the broker.
            task_id: Current task id injected by the broker (may be None).

        Returns:
            ToolResult describing produced artifacts or an honest error/degraded result.
        """
        dataset_id: str = str(args.get("dataset_id") or "").strip()
        if not dataset_id:
            return ToolResult(
                data={"status": "error", "reason": "dataset_id is required"},
                summary="data.run: dataset_id is required",
            )

        code: str = str(args.get("code") or "").strip()
        if not code:
            return ToolResult(
                data={"status": "error", "reason": "code is required"},
                summary="data.run: code is required",
            )

        # Validate code before any I/O.
        try:
            _validate_data_code(code)
        except ValueError as exc:
            return ToolResult(
                data={"status": "error", "reason": str(exc), "artifact_ids": []},
                summary=f"data.run: code rejected — {exc}",
            )

        # Create a fresh per-run workspace so stale PNGs never contaminate results.
        # The workspace (incl. the materialized tenant data file) is always removed
        # after the run so tenant data never lingers on shared disk.
        run_id = str(uuid.uuid4())
        workspace = _data_workspace(org_id, run_id)
        try:
            return await self._run_in_workspace(
                code=code, dataset_id=dataset_id, workspace=workspace,
                org_id=org_id, task_id=task_id,
            )
        finally:
            shutil.rmtree(workspace, ignore_errors=True)

    async def _run_in_workspace(
        self, *, code: str, dataset_id: str, workspace: Path, org_id: str, task_id: str | None
    ) -> ToolResult:
        """Materialize the dataset, run the analysis subprocess, and collect artifacts.

        The caller owns ``workspace`` lifecycle (creation and guaranteed cleanup).
        """
        # Materialize the dataset (org-checked).
        try:
            materialized = await _materialize_dataset(dataset_id, org_id, workspace)
        except Exception as exc:
            return ToolResult(
                data={"status": "error", "reason": f"dataset materialization failed: {type(exc).__name__}"},
                summary=f"data.run: failed to load dataset {dataset_id!r}: {exc}",
            )

        if materialized is None:
            return ToolResult(
                data={"status": "error", "reason": "dataset not found or access denied", "artifact_ids": []},
                summary=f"data.run: dataset {dataset_id!r} not found or belongs to a different organization",
            )

        _data_path, data_filename = materialized

        # Build the sandbox environment.
        mpl_config_dir = workspace / ".mpl_config"
        mpl_config_dir.mkdir(exist_ok=True)

        env = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "HOME": str(workspace),
            # Matplotlib must not try to open a display.
            "MPLBACKEND": "Agg",
            "MPLCONFIGDIR": str(mpl_config_dir),
            # Prevent OpenBLAS/OMP from over-allocating virtual memory on import.
            "OPENBLAS_NUM_THREADS": "1",
            "OMP_NUM_THREADS": "1",
        }

        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            "-c",
            code,
            cwd=str(workspace),
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            preexec_fn=_set_data_limits if os.name == "posix" else None,
            # New session/process-group so a timeout can kill the whole tree
            # (the child plus any grandchildren it spawned), not just the child.
            start_new_session=True,
        )

        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                process.communicate(), timeout=DATA_TIMEOUT_SECONDS
            )
            timed_out = False
        except asyncio.TimeoutError:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                process.kill()
            stdout_b, stderr_b = await process.communicate()
            timed_out = True

        stdout_str = stdout_b[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
        stderr_str = stderr_b[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
        returncode = process.returncode
        exec_status = "timeout" if timed_out else ("success" if returncode == 0 else "failure")

        if exec_status == "timeout":
            return ToolResult(
                data={
                    "status": "timeout",
                    "reason": f"analysis code timed out after {DATA_TIMEOUT_SECONDS}s",
                    "stdout": stdout_str,
                    "stderr": stderr_str,
                    "artifact_ids": [],
                },
                summary="data.run: analysis timed out",
            )

        if exec_status == "failure":
            return ToolResult(
                data={
                    "status": "error",
                    "reason": "analysis code exited with non-zero status",
                    "returncode": returncode,
                    "stdout": stdout_str,
                    "stderr": stderr_str[:2000],
                    "artifact_ids": [],
                },
                summary=f"data.run: analysis failed (exit {returncode})",
            )

        # Collect artifacts from the workspace.
        from core.artifacts import save_artifact

        artifact_ids: list[str] = []

        # Chart PNGs saved by the user code (glob workspace root for *.png).
        chart_files = sorted(workspace.glob("*.png"))
        for chart_path in chart_files:
            chart_bytes = chart_path.read_bytes()
            if not chart_bytes:
                continue
            title = f"Chart: {chart_path.stem}"
            artifact_id = await save_artifact(
                chart_bytes,
                kind="image",
                title=title,
                task_id=task_id,
                org_id=org_id,
                mime_type="image/png",
                created_by="data_analysis_connector",
            )
            artifact_ids.append(artifact_id)

        # Stdout / printed tables → report artifact.
        report_text = stdout_str.strip()
        if report_text:
            report_artifact_id = await save_artifact(
                report_text,
                kind="report",
                title="Analysis Report",
                task_id=task_id,
                org_id=org_id,
                mime_type="text/plain",
                created_by="data_analysis_connector",
            )
            artifact_ids.append(report_artifact_id)
        elif not chart_files:
            # No charts and no stdout — honest "no output".
            return ToolResult(
                data={
                    "status": "success",
                    "artifact_ids": [],
                    "stdout_preview": "",
                    "stderr": stderr_str[:500] if stderr_str else "",
                    "note": "analysis produced no output (no printed text and no saved charts)",
                },
                summary="data.run: analysis completed but produced no output",
            )

        chart_count = len(chart_files)
        return ToolResult(
            data={
                "status": "success",
                "artifact_ids": artifact_ids,
                "chart_count": chart_count,
                "stdout_preview": report_text[:500],
                "stderr": stderr_str[:500] if stderr_str else "",
            },
            summary=(
                f"data.run: analysis complete — "
                f"{chart_count} chart(s), "
                f"{'1 report' if report_text else 'no report'}, "
                f"{len(artifact_ids)} artifact(s)"
            ),
        )


data_analysis_connector = DataAnalysisConnector()
