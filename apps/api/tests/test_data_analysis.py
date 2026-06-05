"""Acceptance proof for Phase 7 Task 7: Data analysis workspace.

Tests verify:
1. requirements importable: pandas, matplotlib, numpy can be imported.
2. Dataset create: save a small CSV artifact → POST /datasets → tenant-scoped
   datasets row with parsed schema (column names) + correct row_count.
3. Analyze → artifacts: tool_broker.execute(agent, "data.run", {dataset_id, code})
   where code reads data.csv with pandas and saves a chart + prints a table →
   chart image artifact AND report artifact are created (kind correct, org correct,
   re-readable via read_artifact_content). Broker audited.
4. Sandbox blocks network/subprocess: code with import socket → honest error, no
   artifacts.
5. Cross-org tool call: org-B agent running data.run on org-A's dataset is rejected
   (honest error, no artifacts; org-A's data never materialized).
6. Cross-org HTTP: GET /datasets/{id} from org-B member → 404.
"""
from __future__ import annotations

import io
import os
import socket as _socket
import uuid

import httpx
import pytest
from sqlalchemy import insert, select

import main  # noqa: F401 — triggers startup/registration
from core.auth import create_access_token
from core.db import engine, reflect_table
from core.models import AgentContext


# ---------------------------------------------------------------------------
# DB connectivity guard
# ---------------------------------------------------------------------------


def _db_reachable() -> bool:
    host, _, port_str = (
        os.environ.get(
            "DATABASE_URL",
            "postgresql+asyncpg://chronos:chronos@localhost:5432/chronos",
        )
        .rpartition("@")[-1]
        .partition("/")[0]
        .rpartition(":")
    )
    try:
        with _socket.create_connection((host or "localhost", int(port_str or 5432)), timeout=1):
            return True
    except OSError:
        return False


_requires_db = pytest.mark.skipif(not _db_reachable(), reason="Postgres not reachable")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ORG_COUNTER = iter(range(9000))


def _unique_org() -> str:
    return f"data-org-{next(_ORG_COUNTER)}"


def _make_agent(org_id: str = "default") -> AgentContext:
    return AgentContext(
        id=str(uuid.uuid4()),
        org_id=org_id,
        task_id=str(uuid.uuid4()),
        member_id=str(uuid.uuid4()),
    )


_SAMPLE_CSV = b"name,age,score\nAlice,30,88.5\nBob,25,92.1\nCarol,35,77.3\n"


async def _make_org_and_member() -> tuple[str, str, str]:
    """Create a fresh org + member. Returns (org_id, member_id, jwt_token)."""
    org_id = str(uuid.uuid4())
    member_id = str(uuid.uuid4())
    orgs = await reflect_table("organizations")
    members = await reflect_table("members")
    async with engine.begin() as conn:
        await conn.execute(orgs.insert().values(id=org_id, slug=f"org-{org_id[:8]}", name="Test Org"))
        await conn.execute(
            members.insert().values(
                id=member_id,
                organization_id=org_id,
                email=f"{member_id[:8]}@t.io",
                role="user",
            )
        )
    return org_id, member_id, create_access_token(member_id)


async def _save_csv_artifact(org_id: str, content: bytes = _SAMPLE_CSV) -> str:
    """Save a CSV as an artifact and return its id."""
    from core.artifacts import save_artifact
    return await save_artifact(
        content,
        kind="file",
        title="test_data.csv",
        org_id=org_id,
        mime_type="text/csv",
        created_by="test",
    )


def _patch_broker_infra(monkeypatch):
    """Patch broker's audit, permissions, tool_policy, connector_tier.

    Returns list of audited event types.
    """
    from core import tool_broker as tb

    audited: list[str] = []

    async def fake_log(event_type, actor, action, **kw):
        audited.append(event_type)

    async def fake_check(*a, **k):
        return True

    async def fake_tool_policy(*a, **k):
        return {}

    monkeypatch.setattr(tb.audit, "log", fake_log)
    monkeypatch.setattr(tb.permissions, "check", fake_check)
    monkeypatch.setattr(tb, "tool_policy", fake_tool_policy)
    from unittest.mock import AsyncMock
    monkeypatch.setattr(tb, "connector_tier", AsyncMock(return_value="live"))
    return audited


# ---------------------------------------------------------------------------
# Test 1 — requirements importable
# ---------------------------------------------------------------------------


def test_imports_available():
    """Assert pandas, matplotlib, numpy can be imported in this process."""
    import pandas  # noqa: F401
    import matplotlib  # noqa: F401
    import numpy  # noqa: F401

    assert pandas.__version__
    assert matplotlib.__version__
    assert numpy.__version__


# ---------------------------------------------------------------------------
# Test 2 — Dataset create via HTTP
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@_requires_db
async def test_dataset_create_http():
    """POST /datasets creates a tenant-scoped datasets row with schema + row_count."""
    from httpx import AsyncClient, ASGITransport

    org_id, member_id, token = await _make_org_and_member()
    artifact_id = await _save_csv_artifact(org_id)

    async with AsyncClient(transport=ASGITransport(app=main.app), base_url="http://test", follow_redirects=True) as client:
        resp = await client.post(
            "/datasets/",
            json={"source_artifact_id": artifact_id, "name": "My CSV"},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["organization_id"] == org_id
    assert body["row_count"] == 3  # _SAMPLE_CSV has 3 data rows
    col_names = [c["name"] for c in body["schema"]["columns"]]
    assert "name" in col_names
    assert "age" in col_names
    assert "score" in col_names

    # Verify the row is actually in the DB.
    datasets = await reflect_table("datasets")
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                select(datasets).where(
                    datasets.c.id == body["id"],
                    datasets.c.organization_id == org_id,
                )
            )
        ).mappings().first()
    assert row is not None
    assert row["row_count"] == 3

    # Assert schema round-trips through JSONB (not just the in-memory response).
    schema_in_db = row["schema"]
    assert isinstance(schema_in_db, dict), f"Schema in DB is not a dict: {type(schema_in_db)}"
    db_col_names = [c["name"] for c in schema_in_db["columns"]]
    assert "name" in db_col_names, f"'name' column not persisted in JSONB; got: {db_col_names}"


# ---------------------------------------------------------------------------
# Test 3 — Analyze → produces chart + report artifacts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@_requires_db
async def test_analyze_produces_chart_and_report(monkeypatch):
    """data.run with chart-saving + print code produces chart image + report artifacts."""
    org_id = _unique_org()
    agent = _make_agent(org_id)
    audited = _patch_broker_infra(monkeypatch)

    artifact_id = await _save_csv_artifact(org_id)

    # Create a dataset row directly (bypass HTTP; broker test focuses on the connector).
    datasets = await reflect_table("datasets")
    dataset_id = str(uuid.uuid4())
    async with engine.begin() as conn:
        await conn.execute(
            insert(datasets).values(
                id=dataset_id,
                organization_id=org_id,
                source_artifact_id=artifact_id,
                name="test",
                schema={"columns": [{"name": "name", "dtype": "object"}]},
                row_count=3,
                status="ready",
                created_by="test",
            )
        )

    analysis_code = """
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

df = pd.read_csv('data.csv')
print(df.to_string())

fig, ax = plt.subplots()
ax.bar(df['name'], df['score'])
ax.set_title('Scores')
plt.savefig('chart_1.png')
plt.close()
"""

    from core import tool_broker

    result = await tool_broker.execute(agent, "data.run", {"dataset_id": dataset_id, "code": analysis_code})

    assert result.data["status"] == "success", f"Expected success, got: {result.data}"

    artifact_ids = result.data["artifact_ids"]
    assert len(artifact_ids) >= 2, f"Expected at least 2 artifacts (chart + report), got: {artifact_ids}"

    # Broker was audited.
    assert "tool_call" in audited
    assert "tool_result" in audited

    # Verify artifacts are readable and belong to the correct org.
    from core.artifacts import get_artifact, read_artifact_content

    kinds = {}
    for aid in artifact_ids:
        meta = await get_artifact(aid)
        assert meta is not None, f"Artifact {aid} not found"
        assert str(meta["organization_id"]) == str(org_id), "Artifact belongs to wrong org"
        content = await read_artifact_content(aid)
        assert content is not None and len(content) > 0, f"Artifact {aid} has no content"
        kinds[meta["kind"]] = aid

    assert "image" in kinds, f"No chart image artifact found; kinds={list(kinds)}"
    assert "report" in kinds, f"No report artifact found; kinds={list(kinds)}"

    # Chart is valid PNG bytes.
    from core.artifacts import read_artifact_content as rac
    chart_bytes = await rac(kinds["image"])
    assert chart_bytes and chart_bytes[:4] == b"\x89PNG", "Chart artifact is not a PNG"


# ---------------------------------------------------------------------------
# Test 4 — Sandbox blocks network import
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@_requires_db
async def test_sandbox_blocks_network_import(monkeypatch):
    """Code with 'import socket' is rejected before subprocess launch; no artifacts."""
    org_id = _unique_org()
    agent = _make_agent(org_id)
    _patch_broker_infra(monkeypatch)

    artifact_id = await _save_csv_artifact(org_id)
    datasets = await reflect_table("datasets")
    dataset_id = str(uuid.uuid4())
    async with engine.begin() as conn:
        await conn.execute(
            insert(datasets).values(
                id=dataset_id,
                organization_id=org_id,
                source_artifact_id=artifact_id,
                name="blocked-test",
                schema={"columns": []},
                row_count=3,
                status="ready",
                created_by="test",
            )
        )

    bad_code = "import socket\nprint(socket.gethostbyname('example.com'))"

    from core import tool_broker

    result = await tool_broker.execute(agent, "data.run", {"dataset_id": dataset_id, "code": bad_code})

    assert result.data["status"] == "error", f"Expected error, got: {result.data['status']}"
    assert "reject" in result.data.get("reason", "").lower() or "forbidden" in result.data.get("reason", "").lower() or "unsafe" in result.data.get("reason", "").lower()
    assert result.data.get("artifact_ids", []) == [], "Should produce no artifacts when rejected"


@pytest.mark.asyncio
@_requires_db
async def test_sandbox_blocks_requests_import(monkeypatch):
    """Code with 'import requests' is also rejected."""
    org_id = _unique_org()
    agent = _make_agent(org_id)
    _patch_broker_infra(monkeypatch)

    artifact_id = await _save_csv_artifact(org_id)
    datasets = await reflect_table("datasets")
    dataset_id = str(uuid.uuid4())
    async with engine.begin() as conn:
        await conn.execute(
            insert(datasets).values(
                id=dataset_id,
                organization_id=org_id,
                source_artifact_id=artifact_id,
                name="blocked-test-2",
                schema={"columns": []},
                row_count=3,
                status="ready",
                created_by="test",
            )
        )

    bad_code = "import requests\nprint(requests.get('http://example.com').text)"

    from core import tool_broker

    result = await tool_broker.execute(agent, "data.run", {"dataset_id": dataset_id, "code": bad_code})

    assert result.data["status"] == "error"
    assert result.data.get("artifact_ids", []) == []


# ---------------------------------------------------------------------------
# Test 5 — Cross-org tool call: org-B agent on org-A dataset → rejected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@_requires_db
async def test_cross_org_tool_call_rejected(monkeypatch):
    """Org-B agent calling data.run on org-A's dataset gets an error; no materialization."""
    org_a = _unique_org()
    org_b = _unique_org()

    _patch_broker_infra(monkeypatch)

    artifact_id = await _save_csv_artifact(org_a)
    datasets = await reflect_table("datasets")
    dataset_id = str(uuid.uuid4())
    async with engine.begin() as conn:
        await conn.execute(
            insert(datasets).values(
                id=dataset_id,
                organization_id=org_a,  # belongs to org A
                source_artifact_id=artifact_id,
                name="org-a-data",
                schema={"columns": []},
                row_count=3,
                status="ready",
                created_by="test",
            )
        )

    # Org B's agent tries to analyze org A's dataset.
    agent_b = _make_agent(org_b)

    from core import tool_broker

    code = "import pandas as pd; df = pd.read_csv('data.csv'); print(df.head())"
    result = await tool_broker.execute(agent_b, "data.run", {"dataset_id": dataset_id, "code": code})

    assert result.data["status"] == "error"
    assert "not found" in result.data.get("reason", "").lower() or "denied" in result.data.get("reason", "").lower()
    assert result.data.get("artifact_ids", []) == []


# ---------------------------------------------------------------------------
# Test 6 — Cross-org HTTP: GET /datasets/{id} from org-B → 404
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@_requires_db
async def test_cross_org_http_get_404():
    """GET /datasets/{id} from a different org returns 404."""
    from httpx import AsyncClient, ASGITransport

    org_a_id2, member_a_id2, token_a2 = await _make_org_and_member()
    org_b_id, member_b_id, token_b = await _make_org_and_member()

    artifact_id2 = await _save_csv_artifact(org_a_id2)

    async with AsyncClient(transport=ASGITransport(app=main.app), base_url="http://test", follow_redirects=True) as client:
        # Org A creates a dataset.
        resp = await client.post(
            "/datasets/",
            json={"source_artifact_id": artifact_id2},
            headers={"Authorization": f"Bearer {token_a2}"},
        )
        assert resp.status_code == 200, resp.text
        dataset_id = resp.json()["id"]

        # Org B tries to GET it.
        resp_b = await client.get(
            f"/datasets/{dataset_id}",
            headers={"Authorization": f"Bearer {token_b}"},
        )
        assert resp_b.status_code == 404, f"Expected 404 for cross-org GET, got {resp_b.status_code}"


# ---------------------------------------------------------------------------
# Test 7 — HTTP analyze endpoint wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@_requires_db
async def test_analyze_endpoint_http(monkeypatch):
    """POST /datasets/{id}/analyze via HTTP returns artifact_ids and status=success."""
    from httpx import AsyncClient, ASGITransport

    org_id, member_id, token = await _make_org_and_member()
    artifact_id = await _save_csv_artifact(org_id)

    # Patch broker infra so the test DB/redis doesn't need rate-limit warmth.
    # Note: monkeypatch on the module-level objects works with ASGI too because
    # the router imports `tool_broker` at call time.
    import core.tool_broker as tb
    from unittest.mock import AsyncMock

    async def fake_log(event_type, actor, action, **kw):
        pass

    async def fake_check(*a, **k):
        return True

    async def fake_tool_policy(*a, **k):
        return {}

    monkeypatch.setattr(tb.audit, "log", fake_log)
    monkeypatch.setattr(tb.permissions, "check", fake_check)
    monkeypatch.setattr(tb, "tool_policy", fake_tool_policy)
    monkeypatch.setattr(tb, "connector_tier", AsyncMock(return_value="live"))

    analysis_code = (
        "import pandas as pd\n"
        "df = pd.read_csv('data.csv')\n"
        "print(df.head().to_string())\n"
    )

    async with AsyncClient(transport=ASGITransport(app=main.app), base_url="http://test", follow_redirects=True) as client:
        # Create dataset.
        resp = await client.post(
            "/datasets/",
            json={"source_artifact_id": artifact_id},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text
        dataset_id = resp.json()["id"]

        # Run analysis.
        resp2 = await client.post(
            f"/datasets/{dataset_id}/analyze",
            json={"code": analysis_code},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp2.status_code == 200, resp2.text
    body2 = resp2.json()
    assert body2["status"] == "success", f"Expected success, got: {body2}"
    # Should have at least a report artifact (stdout is non-empty).
    assert len(body2["artifact_ids"]) >= 1, f"Expected at least 1 artifact; got: {body2['artifact_ids']}"
    assert "stdout_preview" in body2


# ---------------------------------------------------------------------------
# Regression tests: inline tools + runtime + doc parsing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@_requires_db
async def test_inline_tools_no_regression():
    """Importing tool_registry and tool_manifest still works after DATA_RUN addition."""
    from runtime.tool_registry import ALL_TOOLS, SUBAGENT_TOOLS, INLINE_CHAT_TOOLS, tool_name

    all_names = {tool_name(t) for t in ALL_TOOLS}
    sub_names = {tool_name(t) for t in SUBAGENT_TOOLS}
    inline_names = {tool_name(t) for t in INLINE_CHAT_TOOLS}

    assert "data__run" in all_names
    assert "data__run" in sub_names
    assert "data__run" in inline_names

    # Existing tools still present.
    assert "browser__search" in all_names
    assert "image__generate" in all_names
    assert "voice__transcribe" in all_names


@pytest.mark.parametrize("bad", [
    "import importlib\nimportlib.import_module('socket')",
    "import importlib as il\nil.import_module('subprocess')",
    "from importlib import import_module\nimport_module('socket')",
    "import builtins\nbuiltins.__import__('socket')",
    "from builtins import __import__",
    "import httpx",
    "import urllib.request",
    "import subprocess",
    "import multiprocessing",
    "import ctypes",
    "__import__('socket')",
    "open('/etc/passwd').read()",
    "import os\nos.system('id')",
])
def test_validate_data_code_blocks_escape_vectors(bad):
    """The forbidden-pattern validator rejects dynamic-import bypasses, network,
    subprocess, ctypes, absolute-path open, and shell execution."""
    from connectors.data_analysis import _validate_data_code
    with pytest.raises(ValueError):
        _validate_data_code(bad)


def test_validate_data_code_allows_data_libs():
    """Legitimate pandas/matplotlib/numpy analysis code is allowed."""
    from connectors.data_analysis import _validate_data_code
    good = (
        "import pandas as pd\nimport numpy as np\nimport matplotlib\n"
        "matplotlib.use('Agg')\nimport matplotlib.pyplot as plt\n"
        "df = pd.read_csv('data.csv')\nplt.plot(df.index, df.iloc[:,0])\nplt.savefig('chart_1.png')\n"
        "print(df.describe())"
    )
    _validate_data_code(good)  # must not raise
