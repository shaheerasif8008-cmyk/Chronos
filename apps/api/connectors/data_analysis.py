"""Data analysis connector — routes through tool_broker.execute only.

Executes Python analysis code (pandas/matplotlib/numpy) against a materialized
dataset CSV, captures stdout + produced chart PNGs, and persists them as
artifacts. Production uses an ephemeral E2B sandbox; only development/test may
use the resource-limited API-host subprocess. All connector logic lives here;
no business logic belongs in the router.

Sandbox rules:
- Production E2B sandboxes deny internet access by default and are destroyed per call.
- The development subprocess runs with -I (isolated; loads venv site-packages but not user-site or PYTHONPATH).
- pandas, matplotlib, numpy are allowed.
- Network access (socket/requests/httpx/urllib) is blocked.
- subprocess/multiprocessing/ctypes are blocked.
- os.system/popen/exec are blocked.
- Absolute-path open('/...') is blocked.
- MPLBACKEND=Agg: no display dependency.
- MPLCONFIGDIR: writable workspace subdirectory.
- OPENBLAS_NUM_THREADS=1, OMP_NUM_THREADS=1: prevent import-time virtual memory exhaustion.
- RLIMIT_CPU = 30 s, RLIMIT_AS = 2 GB, RLIMIT_FSIZE = 25 MB.
- Wall-clock timeout = 45 s. The additional cold-start budget prevents a fresh
  matplotlib font cache from consuming the entire execution window.

Dataset materialization is org-checked BEFORE any file is written.
"""
from __future__ import annotations

import asyncio
import io
import os
import re
import resource
import shutil
import signal
import sys
import uuid
from pathlib import Path
from typing import Any

from connectors.e2b_runtime import (
    RuntimeUnavailable,
    SANDBOX_ROOT,
    SandboxRuntime,
    default_runtime,
)
from core.config import settings
from core.models import ToolResult
from core.execution_boundary import api_host_execution_allowed, unavailable_host_execution_result

WORKSPACE_ROOT = Path("/tmp/chronos_task_workspaces")
MAX_CODE_BYTES = 128_000
MAX_OUTPUT_BYTES = 128_000
DATA_TIMEOUT_SECONDS = 45
DATA_RLIMIT_CPU = 30
DATA_RLIMIT_AS = 2 * 1024 * 1024 * 1024   # 2 GB
DATA_RLIMIT_FSIZE = 25 * 1024 * 1024       # 25 MB
# RLIMIT_NPROC counts ALL processes/threads of the UID, not just this subprocess.
# On shared hosts (CI runners) the user already holds hundreds of threads, so a
# tight cap blocks even single-thread spawns (e.g. matplotlib's font manager:
# "can't start new thread"). 4096 still contains a runaway fork bomb.
DATA_RLIMIT_NPROC = 4096
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
    r"\bio\.open\s*\(\s*['\"]/",
    r"\bPath\s*\(\s*['\"]/",
    r"\bpathlib\.Path\s*\(\s*['\"]/",
    r"\bos\.(listdir|scandir|walk|stat|lstat|readlink)\s*\(\s*['\"]/",
    # Block shell execution and forking
    r"\bos\.(system|popen|spawn|exec|fork|kill|killpg)",
    # Block arbitrary code execution / introspection escapes
    r"\beval\s*\(",
    r"\bexec\s*\(",
    r"\bcompile\s*\(",
    r"\bbreakpoint\s*\(",
    r"__builtins__",
    r"__subclasses__",
    r"__bases__",
    r"__mro__",
    r"__globals__",
    r"\.\./",            # relative path traversal out of the run workspace
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
    """Load the dataset source artifact and normalize it to ``data.csv``.

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

    # 3. Normalize every advertised input type to the stable data.csv contract
    # used by the default analysis and documented examples. Writing XLSX bytes
    # to a .csv filename produces a delayed, confusing parser failure inside the
    # sandbox; normalization fails visibly before any untrusted code executes.
    mime = str(artifact_meta.get("mime_type", "") or "")
    title = str(artifact_meta.get("title", "") or "")
    ext = title.rsplit(".", 1)[-1].lower() if "." in title else ""
    filename = "data.csv"
    dest = workspace / filename
    if "json" in mime.lower() or ext == "json":
        try:
            import pandas as pd

            pd.read_json(io.BytesIO(content)).to_csv(dest, index=False)
        except Exception as exc:
            raise ValueError("dataset JSON could not be normalized") from exc
    elif "xlsx" in mime.lower() or "spreadsheet" in mime.lower() or ext == "xlsx":
        try:
            import pandas as pd

            pd.read_excel(io.BytesIO(content)).to_csv(dest, index=False)
        except Exception as exc:
            raise ValueError("dataset workbook could not be normalized") from exc
    else:
        dest.write_bytes(content)
    return dest, filename


async def _analysis_result(
    *,
    exec_status: str,
    returncode: int | None,
    stdout_str: str,
    stderr_str: str,
    charts: list[tuple[str, bytes]],
    task_id: str | None,
    org_id: str,
    isolated: bool,
    member_id: str = "data_analysis_connector",
) -> ToolResult:
    """Build the stable data.run envelope and persist generated artifacts."""

    boundary = (
        {"execution_boundary": "isolated_runtime", "host_execution": False}
        if isolated
        else {}
    )
    if exec_status == "timeout":
        return ToolResult(
            data={
                "status": "timeout",
                "reason": f"analysis code timed out after {DATA_TIMEOUT_SECONDS}s",
                "stdout": stdout_str,
                "stderr": stderr_str,
                "artifact_ids": [],
                **boundary,
            },
            summary="data.run: analysis timed out",
        )

    if exec_status != "success":
        return ToolResult(
            data={
                "status": "error",
                "reason": "analysis code exited with non-zero status",
                "returncode": returncode,
                "stdout": stdout_str,
                "stderr": stderr_str[:2000],
                "artifact_ids": [],
                **boundary,
            },
            summary=f"data.run: analysis failed (exit {returncode})",
        )

    from core.artifacts import save_artifact

    artifact_ids: list[str] = []
    nonempty_charts = [(name, content) for name, content in charts if content]
    for name, chart_bytes in nonempty_charts:
        artifact_id = await save_artifact(
            chart_bytes,
            kind="image",
            title=f"Chart: {Path(name).stem}",
            task_id=task_id,
            org_id=org_id,
            mime_type="image/png",
            created_by=member_id,
        )
        artifact_ids.append(artifact_id)

    report_text = stdout_str.strip()
    if report_text:
        report_artifact_id = await save_artifact(
            report_text,
            kind="report",
            title="Analysis Report",
            task_id=task_id,
            org_id=org_id,
            mime_type="text/plain",
            created_by=member_id,
        )
        artifact_ids.append(report_artifact_id)
    elif not nonempty_charts:
        return ToolResult(
            data={
                "status": "success",
                "artifact_ids": [],
                "stdout_preview": "",
                "stderr": stderr_str[:500] if stderr_str else "",
                "note": "analysis produced no output (no printed text and no saved charts)",
                **boundary,
            },
            summary="data.run: analysis completed but produced no output",
        )

    chart_count = len(nonempty_charts)
    return ToolResult(
        data={
            "status": "success",
            "artifact_ids": artifact_ids,
            "chart_count": chart_count,
            "stdout_preview": report_text[:500],
            "stderr": stderr_str[:500] if stderr_str else "",
            **boundary,
        },
        summary=(
            f"data.run: analysis complete — {chart_count} chart(s), "
            f"{'1 report' if report_text else 'no report'}, "
            f"{len(artifact_ids)} artifact(s)"
        ),
    )


class DataAnalysisConnector:
    """Connector for the ``data.run`` tool."""

    def __init__(self, runtime: SandboxRuntime | None = None) -> None:
        self._runtime = runtime

    def _isolated_runtime(self) -> SandboxRuntime | None:
        return self._runtime if self._runtime is not None else default_runtime()

    async def execute(self, tool: str, args: dict[str, Any]) -> ToolResult:
        """Execute a data analysis tool call.

        Args:
            tool: Must be "data.run".
            args: Tool arguments, including broker-injected keys
                ``__connector_tier``, ``__org_id``, ``__task_id``, and
                ``__member_id``.

        Returns:
            ToolResult with artifact ids for generated charts/reports, plus
            stdout preview. Honest error ToolResult when sandbox fails or code
            is rejected.
        """
        args.pop("__connector_tier", None)
        org_id: str = str(args.pop("__org_id", "default") or "default")
        task_id: str | None = args.pop("__task_id", None)
        member_id = str(
            args.pop("__member_id", "data_analysis_connector")
            or "data_analysis_connector"
        )

        if tool != "data.run":
            raise ValueError(f"Unknown data tool: {tool}")
        if settings.is_production:
            runtime = self._isolated_runtime()
            if runtime is None:
                return ToolResult(
                    data={
                        "status": "unavailable",
                        "reason": "data.run requires the isolated E2B runtime; set E2B_API_KEY.",
                        "artifact_ids": [],
                        "execution_boundary": "isolated_runtime_required",
                        "host_execution": False,
                    },
                    summary="data.run unavailable: isolated runtime required",
                )
            return await self._run(
                args,
                org_id=org_id,
                task_id=task_id,
                member_id=member_id,
                isolated_runtime=runtime,
            )
        if not api_host_execution_allowed():
            return unavailable_host_execution_result(tool)

        return await self._run(
            args,
            org_id=org_id,
            task_id=task_id,
            member_id=member_id,
        )

    async def _run(
        self,
        args: dict[str, Any],
        *,
        org_id: str,
        task_id: str | None,
        member_id: str = "data_analysis_connector",
        isolated_runtime: SandboxRuntime | None = None,
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
            if isolated_runtime is not None:
                return await self._run_in_isolated_workspace(
                    code=code,
                    dataset_id=dataset_id,
                    workspace=workspace,
                    org_id=org_id,
                    task_id=task_id,
                    member_id=member_id,
                    runtime=isolated_runtime,
                )
            return await self._run_in_workspace(
                code=code, dataset_id=dataset_id, workspace=workspace,
                org_id=org_id, task_id=task_id, member_id=member_id,
            )
        finally:
            shutil.rmtree(workspace, ignore_errors=True)

    async def _run_in_isolated_workspace(
        self,
        *,
        code: str,
        dataset_id: str,
        workspace: Path,
        org_id: str,
        task_id: str | None,
        runtime: SandboxRuntime,
        member_id: str = "data_analysis_connector",
    ) -> ToolResult:
        """Upload tenant-checked input and execute analysis in ephemeral E2B."""

        try:
            materialized = await _materialize_dataset(dataset_id, org_id, workspace)
        except Exception as exc:
            return ToolResult(
                data={
                    "status": "error",
                    "reason": f"dataset materialization failed: {type(exc).__name__}",
                    "artifact_ids": [],
                    "execution_boundary": "isolated_runtime",
                    "host_execution": False,
                },
                summary=f"data.run: failed to load dataset {dataset_id!r}: {exc}",
            )

        if materialized is None:
            return ToolResult(
                data={
                    "status": "error",
                    "reason": "dataset not found or access denied",
                    "artifact_ids": [],
                    "execution_boundary": "isolated_runtime",
                    "host_execution": False,
                },
                summary=f"data.run: dataset {dataset_id!r} not found or belongs to a different organization",
            )

        data_path, data_filename = materialized
        bootstrap = (
            "import os\n"
            "os.environ['MPLBACKEND'] = 'Agg'\n"
            "os.environ['MPLCONFIGDIR'] = '/tmp/matplotlib'\n"
            "os.environ['OPENBLAS_NUM_THREADS'] = '1'\n"
            "os.environ['OMP_NUM_THREADS'] = '1'\n"
        )
        sandbox_id: str | None = None
        chart_outputs: list[tuple[str, bytes]] = []
        try:
            sandbox_id = await runtime.create(
                timeout_seconds=min(settings.e2b_sandbox_timeout_seconds, 120),
                metadata={"org": org_id, "task": task_id or "manual", "tool": "data.run"},
            )
            await runtime.write(
                sandbox_id,
                f"{SANDBOX_ROOT}/{data_filename}",
                data_path.read_bytes(),
            )
            await runtime.write(
                sandbox_id,
                f"{SANDBOX_ROOT}/analysis.py",
                f"{bootstrap}\n{code}\n".encode("utf-8"),
            )
            result = await runtime.run(
                sandbox_id,
                "python3 analysis.py",
                cwd=SANDBOX_ROOT,
                timeout_seconds=DATA_TIMEOUT_SECONDS,
            )
            if result.get("status") == "success":
                entries = await runtime.list(sandbox_id, SANDBOX_ROOT)
                for entry in entries:
                    if entry.get("type") == "directory":
                        continue
                    name = Path(str(entry.get("name") or "")).name
                    if not name.lower().endswith(".png"):
                        continue
                    if int(entry.get("size") or 0) > DATA_RLIMIT_FSIZE:
                        continue
                    # A bounded output count prevents a sandbox from forcing an
                    # unbounded series of provider reads.
                    if len(chart_outputs) >= 20:
                        break
                    content = await runtime.read(sandbox_id, f"{SANDBOX_ROOT}/{name}")
                    if len(content) <= DATA_RLIMIT_FSIZE:
                        chart_outputs.append((name, content))
        except RuntimeUnavailable as exc:
            return ToolResult(
                data={
                    "status": "unavailable",
                    "reason": f"isolated data runtime unavailable: {exc}",
                    "artifact_ids": [],
                    "execution_boundary": "isolated_runtime_required",
                    "host_execution": False,
                },
                summary="data.run unavailable: isolated runtime failed",
            )
        except Exception as exc:  # noqa: BLE001 - provider errors vary
            return ToolResult(
                data={
                    "status": "failure",
                    "reason": f"isolated data execution failed: {type(exc).__name__}",
                    "artifact_ids": [],
                    "execution_boundary": "isolated_runtime",
                    "host_execution": False,
                },
                summary="data.run failed in isolated runtime",
            )
        finally:
            if sandbox_id is not None:
                try:
                    await runtime.kill(sandbox_id)
                except Exception:
                    # E2B's TTL remains the cleanup backstop.
                    pass

        stdout_b = str(result.get("stdout") or "").encode("utf-8")
        stderr_b = str(result.get("stderr") or "").encode("utf-8")
        return await _analysis_result(
            exec_status=str(result.get("status") or "failure"),
            returncode=result.get("returncode"),
            stdout_str=stdout_b[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace"),
            stderr_str=stderr_b[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace"),
            charts=chart_outputs,
            task_id=task_id,
            org_id=org_id,
            isolated=True,
            member_id=member_id,
        )

    async def _run_in_workspace(
        self,
        *,
        code: str,
        dataset_id: str,
        workspace: Path,
        org_id: str,
        task_id: str | None,
        member_id: str = "data_analysis_connector",
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

        charts = [
            (chart_path.name, chart_path.read_bytes())
            for chart_path in sorted(workspace.glob("*.png"))
        ]
        return await _analysis_result(
            exec_status=exec_status,
            returncode=returncode,
            stdout_str=stdout_str,
            stderr_str=stderr_str,
            charts=charts,
            task_id=task_id,
            org_id=org_id,
            isolated=False,
            member_id=member_id,
        )


data_analysis_connector = DataAnalysisConnector()
