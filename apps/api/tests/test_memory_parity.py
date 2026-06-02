"""Phase 4 — Memory Parity proof.

Backend behavior proven against the isolated test DB. Covers the control center
(archive/pin/sensitive/scope/merge/search/export/import/usage), conflict &
staleness detection, and privacy controls — plus the retrieval-seam enforcement
that makes archived/superseded/disabled memory stop being recalled.
"""
import os
import socket
import uuid

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


_CONST_VECTOR = [0.1] * 1536


async def _fake_embed(text: str) -> list[float]:
    # Deterministic, offline, correct-dimension embedding so create + retrieve work
    # without a provider. Constant vector => every row matches; the SQL filters
    # (archived/superseded/disabled) are what decide what comes back.
    return list(_CONST_VECTOR)


@pytest.fixture(autouse=True)
def _patch_embed(monkeypatch):
    import core.memory as memory_mod
    import core.memory_writes as writes_mod

    monkeypatch.setattr(memory_mod, "embed", _fake_embed)
    monkeypatch.setattr(writes_mod, "embed", _fake_embed)


def _member(org_id: str):
    from core.models import Member

    return Member(id=str(uuid.uuid4()), organization_id=org_id, email="m@t.io", role="owner")


def _ctx(member):
    from core.models import RequesterContext

    return RequesterContext.from_member(member)


async def _add(member, content, *, scope="personal", scope_id=None, source="explicit"):
    from core.memory_writes import create_memory_entry

    return await create_memory_entry(
        content=content,
        requester_context=_ctx(member),
        source=source,
        scope=scope,
        scope_id=scope_id or member.id,
        importance_score=0.8,
        created_by=member.id,
    )


# --------------------------------------------------------------------------- #
@_requires_db
@pytest.mark.asyncio
async def test_archived_and_superseded_excluded_from_retrieval():
    from core import memory as memory_mod
    from core.memory_control import archive_memory, merge_memories

    org = f"test-{uuid.uuid4().hex[:8]}"
    member = _member(org)
    keep = await _add(member, "alpha keep this durable fact")
    arch = await _add(member, "beta archived fact")
    dup = await _add(member, "gamma duplicate fact")

    # Archive one, supersede another via merge.
    assert await archive_memory(arch, member) is True
    assert await merge_memories(member, primary_id=keep, duplicate_ids=[dup]) == 1

    results = await memory_mod.retrieve("fact", _ctx(member))
    ids = {m.id for m in results}
    assert keep in ids
    assert arch not in ids
    assert dup not in ids


@_requires_db
@pytest.mark.asyncio
async def test_pinned_memory_outranks_peers():
    # Unit-level ranking proof: a pinned row beats an equally-similar unpinned one.
    from core.memory import _rank_memory_rows

    rows = [
        {"id": "plain", "distance": 0.2, "importance_score": 0.5, "source": "explicit", "is_pinned": False},
        {"id": "pinned", "distance": 0.2, "importance_score": 0.5, "source": "explicit", "is_pinned": True},
    ]
    ranked = _rank_memory_rows(rows)
    assert ranked[0]["id"] == "pinned"


@_requires_db
@pytest.mark.asyncio
async def test_conflict_detection_and_resolution():
    from core import memory as memory_mod
    from core.memory_control import detect_conflicts, resolve_conflict

    org = f"test-{uuid.uuid4().hex[:8]}"
    member = _member(org)
    a = await _add(member, "client prefers morning calls and email follow ups")
    b = await _add(member, "client prefers morning calls and email follow ups please")
    await _add(member, "completely unrelated invoice numbering scheme detail")

    conflicts = await detect_conflicts(member)
    pair = next((c for c in conflicts if {c["stale_id"], c["survivor_id"]} == {a, b}), None)
    assert pair is not None
    assert pair["similarity"] >= 0.5

    assert await resolve_conflict(member, stale_id=pair["stale_id"], survivor_id=pair["survivor_id"]) is True
    # Stale one no longer retrieved.
    results = await memory_mod.retrieve("client preferences", _ctx(member))
    assert pair["stale_id"] not in {m.id for m in results}


@_requires_db
@pytest.mark.asyncio
async def test_privacy_disable_blocks_retrieval_and_is_scoped():
    from core import memory as memory_mod
    from core.memory_control import is_memory_enabled, set_memory_policy
    from core.models import RequesterContext

    org = f"test-{uuid.uuid4().hex[:8]}"
    member = _member(org)
    project_id = str(uuid.uuid4())
    await _add(member, "fact that should be hidden when memory is off")

    ctx_project = RequesterContext(org_id=org, member_id=member.id, project_id=project_id, role="owner")
    # Enabled by default.
    assert await is_memory_enabled(org_id=org, project_id=project_id, member_id=member.id) is True
    assert len(await memory_mod.retrieve("fact", ctx_project)) >= 1

    # Disable for the project -> retrieval returns nothing in that project context.
    await set_memory_policy(member, scope="project", scope_id=project_id, enabled=False)
    assert await is_memory_enabled(org_id=org, project_id=project_id, member_id=member.id) is False
    assert await memory_mod.retrieve("fact", ctx_project) == []

    # A different project is unaffected (policy is scoped).
    other_ctx = RequesterContext(org_id=org, member_id=member.id, project_id=str(uuid.uuid4()), role="owner")
    assert await is_memory_enabled(org_id=org, project_id=other_ctx.project_id, member_id=member.id) is True


@_requires_db
@pytest.mark.asyncio
async def test_usage_log_records_retrieved_memories():
    from core import memory as memory_mod
    from core.memory_control import list_memory_usage

    org = f"test-{uuid.uuid4().hex[:8]}"
    member = _member(org)
    mid = await _add(member, "usage tracked durable fact about the account")

    results = await memory_mod.retrieve("account fact", _ctx(member))
    assert mid in {m.id for m in results}
    usage = await list_memory_usage(mid, member)
    assert len(usage) >= 1
    assert usage[0]["used_by"] == member.id


@_requires_db
@pytest.mark.asyncio
async def test_export_import_round_trip():
    from core.memory_control import export_memories, import_memories, list_memories

    org_a = f"a-{uuid.uuid4().hex[:8]}"
    org_b = f"b-{uuid.uuid4().hex[:8]}"
    member_a = _member(org_a)
    member_b = _member(org_b)
    await _add(member_a, "exportable fact one", scope="org", scope_id=org_a)
    await _add(member_a, "exportable fact two", scope="org", scope_id=org_a)

    exported = await export_memories(member_a)
    assert len(exported) >= 2

    ids = await import_memories(member_b, exported)
    assert len(ids) == len(exported)
    imported = await list_memories(member_b)
    contents = {r["content"] for r in imported}
    assert "exportable fact one" in contents and "exportable fact two" in contents
    assert all(r["source"] == "imported" for r in imported)


@_requires_db
@pytest.mark.asyncio
async def test_control_flags_are_tenant_scoped():
    from core.memory_control import archive_memory, set_pinned, set_sensitive

    org_a = f"a-{uuid.uuid4().hex[:8]}"
    org_b = f"b-{uuid.uuid4().hex[:8]}"
    member_a = _member(org_a)
    mid = await _add(member_a, "tenant A private memory", scope="org", scope_id=org_a)

    foreign = _member(org_b)
    assert await archive_memory(mid, foreign) is False
    assert await set_pinned(mid, foreign, pinned=True) is False
    assert await set_sensitive(mid, foreign, sensitive=True) is False
    # Owner can.
    assert await set_sensitive(mid, member_a, sensitive=True) is True


@_requires_db
@pytest.mark.asyncio
async def test_disabled_memory_blocks_explicit_write():
    import pytest as _pytest
    from fastapi import HTTPException

    from core.memory_control import set_memory_policy
    from routers.memory import MemoryCreate, add_memory

    org = f"test-{uuid.uuid4().hex[:8]}"
    member = _member(org)
    # Disable memory for this member.
    await set_memory_policy(member, scope="member", scope_id=member.id, enabled=False)
    with _pytest.raises(HTTPException) as ei:
        await add_memory(MemoryCreate(content="should be blocked", scope="org"), member=member)
    assert ei.value.status_code == 403


@_requires_db
@pytest.mark.asyncio
async def test_disabled_memory_blocks_autonomous_extraction_write(monkeypatch):
    from memory import extraction as extraction_mod
    from core.memory_control import list_memories, set_memory_policy

    org = f"test-{uuid.uuid4().hex[:8]}"
    member = _member(org)
    conversation_id = str(uuid.uuid4())

    async def _should_not_run(*a, **k):  # extraction must return before any model call
        raise AssertionError("complete_json should not be called when memory is disabled")

    monkeypatch.setattr(extraction_mod, "complete_json", _should_not_run)

    await set_memory_policy(member, scope="conversation", scope_id=conversation_id, enabled=False)
    # Returns early (no write, no model call) — no exception raised.
    await extraction_mod.extract_and_save(conversation_id, "u", "a", _ctx(member))
    assert await list_memories(member, include_archived=True, include_superseded=True) == []


@_requires_db
@pytest.mark.asyncio
async def test_search_filter_in_list():
    from core.memory_control import list_memories

    org = f"test-{uuid.uuid4().hex[:8]}"
    member = _member(org)
    await _add(member, "the quarterly board meeting is in March", scope="org", scope_id=org)
    await _add(member, "preferred vendor for catering is Acme", scope="org", scope_id=org)

    hits = await list_memories(member, query="board meeting")
    assert len(hits) == 1
    assert "board meeting" in hits[0]["content"]
