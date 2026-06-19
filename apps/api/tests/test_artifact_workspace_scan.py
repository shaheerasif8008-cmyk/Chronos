from __future__ import annotations

import os
import socket
import uuid
from types import SimpleNamespace

import pytest


def _db_reachable() -> bool:
    host, _, port_str = os.environ.get(
        "DATABASE_URL", "postgresql+asyncpg://chronos:chronos@localhost:5432/chronos"
    ).rpartition("@")[-1].partition("/")[0].rpartition(":")
    port = int(port_str) if port_str.isdigit() else 5432
    try:
        with socket.create_connection((host or "localhost", port), timeout=1):
            return True
    except OSError:
        return False


_requires_db = pytest.mark.skipif(not _db_reachable(), reason="Postgres not reachable")


# ── _RENDERABLE_EXT map (pure unit, no DB) ────────────────────────────────────


class TestRenderableExtensionMap:
    """The renderable map must cover binary office formats so the workspace
    scan recognises them, and the binary-refusal set must contain the same
    extensions so `fs__write` cannot produce corrupt text-as-pptx files."""

    def test_pptx_is_renderable(self):
        from runtime.agent_loop import _RENDERABLE_EXT

        assert ".pptx" in _RENDERABLE_EXT
        kind, mime = _RENDERABLE_EXT[".pptx"]
        assert kind == "presentation"
        assert "presentationml" in mime

    def test_docx_is_renderable(self):
        from runtime.agent_loop import _RENDERABLE_EXT

        assert ".docx" in _RENDERABLE_EXT
        kind, mime = _RENDERABLE_EXT[".docx"]
        assert kind == "document"
        assert "wordprocessingml" in mime

    def test_xlsx_is_renderable(self):
        from runtime.agent_loop import _RENDERABLE_EXT

        assert ".xlsx" in _RENDERABLE_EXT
        kind, mime = _RENDERABLE_EXT[".xlsx"]
        assert kind == "spreadsheet"
        assert "spreadsheetml" in mime

    def test_pdf_is_renderable(self):
        from runtime.agent_loop import _RENDERABLE_EXT

        assert ".pdf" in _RENDERABLE_EXT
        kind, mime = _RENDERABLE_EXT[".pdf"]
        assert kind == "pdf"
        assert mime == "application/pdf"

    def test_text_formats_still_renderable(self):
        """The fix must not regress the text-format surface."""
        from runtime.agent_loop import _RENDERABLE_EXT

        for ext, kind in [(".html", "html"), (".md", "markdown"), (".svg", "image"),
                          (".json", "data"), (".csv", "data"), (".py", "code")]:
            assert ext in _RENDERABLE_EXT, f"Missing renderable: {ext}"
            assert _RENDERABLE_EXT[ext][0] == kind

    def test_binary_ext_set_matches_office_formats(self):
        from runtime.agent_loop import _BINARY_EXT, _RENDERABLE_EXT

        assert _BINARY_EXT == {".pdf", ".pptx", ".docx", ".xlsx"}
        assert _BINARY_EXT.issubset(set(_RENDERABLE_EXT.keys()))


# ── _maybe_create_artifact — binary refusal (no DB) ──────────────────────────


@pytest.mark.asyncio
class TestFsWriteBinaryRejection:
    """`fs__write` is text-only. It must refuse to claim a binary format was
    produced — that would create a corrupt text-as-pptx file in the user's
    downloads. The legitimate path is `code__python`, picked up by the
    workspace scan."""

    async def test_pptx_path_returns_none(self):
        from runtime.agent_loop import _maybe_create_artifact

        task = {"id": str(uuid.uuid4()), "organization_id": "default"}
        args = {"path": "deck.pptx", "content": "not real pptx bytes"}
        assert await _maybe_create_artifact(task, args) is None

    async def test_docx_path_returns_none(self):
        from runtime.agent_loop import _maybe_create_artifact

        task = {"id": str(uuid.uuid4()), "organization_id": "default"}
        assert await _maybe_create_artifact(task, {"path": "r.docx", "content": "x"}) is None

    async def test_pdf_path_returns_none(self):
        from runtime.agent_loop import _maybe_create_artifact

        task = {"id": str(uuid.uuid4()), "organization_id": "default"}
        assert await _maybe_create_artifact(task, {"path": "r.pdf", "content": "x"}) is None

    async def test_xlsx_path_returns_none(self):
        from runtime.agent_loop import _maybe_create_artifact

        task = {"id": str(uuid.uuid4()), "organization_id": "default"}
        assert await _maybe_create_artifact(task, {"path": "s.xlsx", "content": "x"}) is None

    async def test_html_path_still_creates_artifact(self, tmp_path, monkeypatch):
        """The binary refusal must not regress text formats."""
        from runtime.agent_loop import _maybe_create_artifact

        monkeypatch.setattr("core.workspace.WORKSPACE_ROOT", tmp_path)
        task = {"id": str(uuid.uuid4()), "organization_id": "default"}
        args = {"path": "page.html", "content": "<h1>Hi</h1>"}

        result = await _maybe_create_artifact(task, args)
        assert result is not None
        assert result["kind"] == "html"
        assert result["title"] == "page.html"


# ── Tool-returned artifact ids — no DB ───────────────────────────────────────


@pytest.mark.asyncio
async def test_tool_returned_uploaded_document_artifact_surfaces_once(monkeypatch):
    """A doc workflow that returns a filled PDF artifact id should render in chat."""
    from runtime.agent_loop import _execute_tool, _surface_result_artifacts

    async def fake_get_artifact(artifact_id: str):
        rows = {
            "filled-pdf-1": {
                "id": "filled-pdf-1",
                "organization_id": "org-1",
                "title": "filled-intake.pdf",
                "kind": "pdf",
                "mime_type": "application/pdf",
                "size_bytes": 2048,
            },
            "other-tenant": {
                "id": "other-tenant",
                "organization_id": "org-2",
                "title": "private.pdf",
                "kind": "pdf",
                "mime_type": "application/pdf",
                "size_bytes": 4096,
            },
        }
        return rows.get(artifact_id)

    monkeypatch.setattr("core.artifacts.get_artifact", fake_get_artifact)
    emitted: list[dict] = []

    async def fake_execute(agent, broker_name, args):
        assert broker_name == "doc.fill_pdf"
        return SimpleNamespace(
            summary="Filled PDF",
            data={
                "status": "success",
                "artifact_id": "filled-pdf-1",
                "artifact_ids": ["filled-pdf-1", "other-tenant", ""],
            },
        )

    async def fake_emit_activity(task_id, event, actor_id="chronos"):
        emitted.append(event)

    monkeypatch.setattr("runtime.agent_loop.tool_broker.execute", fake_execute)
    monkeypatch.setattr("runtime.agent_loop.emit_activity", fake_emit_activity)

    task = {"id": "task-1", "organization_id": "org-1"}
    result = await _surface_result_artifacts(
        task,
        {
            "artifact_id": "filled-pdf-1",
            "artifact_ids": ["filled-pdf-1", "other-tenant", ""],
        },
    )

    assert result == [
        {
            "artifact_id": "filled-pdf-1",
            "title": "filled-intake.pdf",
            "kind": "pdf",
            "mime_type": "application/pdf",
            "size_bytes": 2048,
        }
    ]

    tool_message = await _execute_tool(
        {"id": "call-1", "name": "doc__fill_pdf", "args_str": "{}"},
        task,
        agent=SimpleNamespace(),
    )

    assert tool_message["artifacts"] == result
    artifact_events = [event for event in emitted if event.get("type") == "artifact"]
    assert artifact_events == [{"type": "artifact", **result[0]}]


# ── _scan_workspace_for_artifacts — DB-requiring ──────────────────────────────


@_requires_db
@pytest.mark.asyncio
class TestWorkspaceArtifactScan:
    """The post-`code__python` scan walks the task workspace, archives any
    renderable file the model just produced, and dedups by (task_id, title)."""

    async def test_scan_finds_newly_written_pptx(self, tmp_path, monkeypatch):
        from runtime.agent_loop import _scan_workspace_for_artifacts

        monkeypatch.setattr("core.workspace.WORKSPACE_ROOT", tmp_path)
        org = f"test-{uuid.uuid4().hex[:8]}"
        task_id = str(uuid.uuid4())

        # Simulate python-pptx writing real binary bytes to the workspace.
        ws = tmp_path / org / task_id
        ws.mkdir(parents=True)
        (ws / "deck.pptx").write_bytes(b"PK\x03\x04fake-pptx-bytes")

        task = {"id": task_id, "organization_id": org}
        new_artifacts = await _scan_workspace_for_artifacts(task)
        assert len(new_artifacts) == 1
        a = new_artifacts[0]
        assert a["kind"] == "presentation"
        assert a["title"] == "deck.pptx"
        assert a["mime_type"] == "application/vnd.openxmlformats-officedocument.presentationml.presentation"
        assert a["artifact_id"]
        assert a["size_bytes"] > 0

    async def test_scan_persists_into_artifacts_table(self, tmp_path, monkeypatch):
        """The scan must persist into the artifacts table, not just return a stub."""
        from core.artifacts import get_artifact
        from runtime.agent_loop import _scan_workspace_for_artifacts

        monkeypatch.setattr("core.workspace.WORKSPACE_ROOT", tmp_path)
        org = f"test-{uuid.uuid4().hex[:8]}"
        task_id = str(uuid.uuid4())

        ws = tmp_path / org / task_id
        ws.mkdir(parents=True)
        (ws / "report.pdf").write_bytes(b"%PDF-1.4 fake")

        task = {"id": task_id, "organization_id": org}
        new_artifacts = await _scan_workspace_for_artifacts(task)
        assert len(new_artifacts) == 1
        aid = new_artifacts[0]["artifact_id"]

        meta = await get_artifact(aid)
        assert meta is not None
        assert meta["title"] == "report.pdf"
        assert str(meta["task_id"]) == task_id

    async def test_scan_dedups_by_relative_path(self, tmp_path, monkeypatch):
        """Re-scanning the same workspace must not double-archive the same file."""
        from runtime.agent_loop import _scan_workspace_for_artifacts

        monkeypatch.setattr("core.workspace.WORKSPACE_ROOT", tmp_path)
        org = f"test-{uuid.uuid4().hex[:8]}"
        task_id = str(uuid.uuid4())

        ws = tmp_path / org / task_id
        ws.mkdir(parents=True)
        (ws / "doc.docx").write_bytes(b"PK\x03\x04fake-docx")

        task = {"id": task_id, "organization_id": org}
        first = await _scan_workspace_for_artifacts(task)
        second = await _scan_workspace_for_artifacts(task)
        assert len(first) == 1
        assert len(second) == 0

    async def test_scan_skips_pycache(self, tmp_path, monkeypatch):
        """Compiled Python in __pycache__ must not leak as a binary artifact."""
        from runtime.agent_loop import _scan_workspace_for_artifacts

        monkeypatch.setattr("core.workspace.WORKSPACE_ROOT", tmp_path)
        org = f"test-{uuid.uuid4().hex[:8]}"
        task_id = str(uuid.uuid4())

        ws = tmp_path / org / task_id
        pycache = ws / "__pycache__"
        pycache.mkdir(parents=True)
        # An .xlsx file (a renderable extension) inside __pycache__ must be ignored.
        (pycache / "module.xlsx").write_bytes(b"PK\x03\x04not-real")

        task = {"id": task_id, "organization_id": org}
        new_artifacts = await _scan_workspace_for_artifacts(task)
        assert new_artifacts == []

    async def test_scan_skips_dotfile_dirs(self, tmp_path, monkeypatch):
        from runtime.agent_loop import _scan_workspace_for_artifacts

        monkeypatch.setattr("core.workspace.WORKSPACE_ROOT", tmp_path)
        org = f"test-{uuid.uuid4().hex[:8]}"
        task_id = str(uuid.uuid4())

        ws = tmp_path / org / task_id
        hidden = ws / ".venv"
        hidden.mkdir(parents=True)
        (hidden / "thing.pptx").write_bytes(b"PK\x03\x04hidden")

        task = {"id": task_id, "organization_id": org}
        new_artifacts = await _scan_workspace_for_artifacts(task)
        assert new_artifacts == []

    async def test_scan_picks_up_new_files_on_second_call(self, tmp_path, monkeypatch):
        """A second file added between scans is picked up; the first is not redelivered."""
        from runtime.agent_loop import _scan_workspace_for_artifacts

        monkeypatch.setattr("core.workspace.WORKSPACE_ROOT", tmp_path)
        org = f"test-{uuid.uuid4().hex[:8]}"
        task_id = str(uuid.uuid4())

        ws = tmp_path / org / task_id
        ws.mkdir(parents=True)
        (ws / "a.pptx").write_bytes(b"PK\x03\x04a")

        task = {"id": task_id, "organization_id": org}
        first = await _scan_workspace_for_artifacts(task)
        assert {a["title"] for a in first} == {"a.pptx"}

        (ws / "b.docx").write_bytes(b"PK\x03\x04b")
        second = await _scan_workspace_for_artifacts(task)
        assert {a["title"] for a in second} == {"b.docx"}

    async def test_scan_ignores_non_renderable_files(self, tmp_path, monkeypatch):
        """Random binaries (e.g. .so, .bin) must not be archived."""
        from runtime.agent_loop import _scan_workspace_for_artifacts

        monkeypatch.setattr("core.workspace.WORKSPACE_ROOT", tmp_path)
        org = f"test-{uuid.uuid4().hex[:8]}"
        task_id = str(uuid.uuid4())

        ws = tmp_path / org / task_id
        ws.mkdir(parents=True)
        (ws / "lib.so").write_bytes(b"\x7fELFfake")
        (ws / "data.bin").write_bytes(b"raw")

        task = {"id": task_id, "organization_id": org}
        assert await _scan_workspace_for_artifacts(task) == []

    async def test_scan_handles_nested_directories(self, tmp_path, monkeypatch):
        """Renderable files in subdirectories must be picked up with relative paths."""
        from runtime.agent_loop import _scan_workspace_for_artifacts

        monkeypatch.setattr("core.workspace.WORKSPACE_ROOT", tmp_path)
        org = f"test-{uuid.uuid4().hex[:8]}"
        task_id = str(uuid.uuid4())

        ws = tmp_path / org / task_id
        sub = ws / "reports"
        sub.mkdir(parents=True)
        (sub / "q1.pptx").write_bytes(b"PK\x03\x04q1")

        task = {"id": task_id, "organization_id": org}
        new_artifacts = await _scan_workspace_for_artifacts(task)
        assert len(new_artifacts) == 1
        assert new_artifacts[0]["title"] == "reports/q1.pptx"
