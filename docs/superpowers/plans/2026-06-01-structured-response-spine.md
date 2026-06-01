# Structured Response Spine — Implementation Plan (Plan 1 of 2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every Chronos chat response carry a persisted, runtime-owned structured envelope (status, artifacts, action verbs, approval state, risks, next action) that survives refresh and renders as cards — proven end-to-end for the `task_complete` and `direct_answer` types.

**Architecture:** One envelope type (`StructuredResponse`) with `response_type` as a discriminator and optional sections — NOT 12 separate schemas. The runtime owns all truth fields (status, approval_status, artifacts, action verbs, sources); a single LLM call (`complete_json`, the codebase's existing structured-output path) produces only the prose fields (summary, key_findings, assumptions, next_action, confidence). A truth guard prevents the model's prose from asserting side effects the runtime didn't perform. The envelope is persisted to a new `messages.structured_response` JSONB column, returned by `GET /conversations/{id}/messages`, streamed over SSE as a `structured_response` event, and rendered as cards that rebuild from stored state on reload.

**Tech Stack:** Python 3.11 / FastAPI / SQLAlchemy Core / Alembic / Pydantic v2 / litellm (`complete_json`) on the backend; Next.js 14 / TypeScript / Tailwind on the frontend. Tests: pytest (`apps/api/tests/`).

**Scope note:** This plan builds the spine plus two response types (`task_complete`, `direct_answer`) and a customization toggle. The remaining ~10 doctrine response types (advisory, research, risk_advisory, failure_recovery, clarification, approval_request, etc.) and their bespoke cards are **Plan 2**, to be written after this plan round-trips. Each plan produces working software on its own.

**What is REUSED, not rebuilt** (do not re-implement these):
- `tool_traces` on `messages` + `TraceRow`/`ReasoningPanel` already implement the Audit/Trace level.
- `artifact_refs` + `ArtifactCard`/`InChatArtifactPanel` already implement artifact cards.
- `messages.approval_state` / `runtime_status` columns already exist (from migration `0017_messages_rich`) but are not consistently populated — this plan's `structured_response` envelope becomes the canonical source; leave the old columns untouched.
- The Approvals screen and `/approvals` API already exist; the new in-chat approval card links to them.

**Genuinely new UI:** status banner, risks/assumptions card, next-action affordance, and an in-chat approval summary card.

---

## File Structure

**Backend (new):**
- `apps/api/core/structured_response.py` — Pydantic envelope + runtime-fact extraction + truth guard + `compose()`. One responsibility: producing a `StructuredResponse`.
- `apps/api/migrations/versions/0023_message_structured_response.py` — adds `messages.structured_response` JSONB.
- `apps/api/tests/test_structured_response.py` — unit + integration tests for the module and the round-trip.

**Backend (modified):**
- `apps/api/runtime/agent_loop.py` — at the `run_loop` final-answer site, build + persist + emit the envelope.
- `apps/api/routers/chat.py` — fast non-loop `stream()` path emits a minimal `direct_answer` envelope; `_save_message` accepts an optional envelope; `_agent_loop_stream` forwards a `structured_response` SSE event.
- `apps/api/core/settings_store.py` — no signature change; a new `response_format` settings section is read via the existing `get_settings_doc`.

**Frontend (modified):**
- `apps/web/app/chat/page.tsx` — `Message.structured_response` type; SSE `structured_response` handler; load envelope from stored messages; render `StatusBanner`, `RisksCard`, `NextActionRow`, `InlineApprovalCard` inside `AssistantMessage`; read `response_format` settings to gate sections.

---

## Data Contract (defined once, referenced by every task)

```python
# apps/api/core/structured_response.py  — the canonical envelope
RESPONSE_TYPES = {"direct_answer", "task_complete"}  # Plan 2 extends this set

STATUSES = {
    "complete", "in_progress", "needs_input", "needs_approval",
    "partial", "blocked", "failed", "cancelled",
}

ACTION_VERBS = {
    "suggested", "drafted", "prepared", "scheduled",
    "sent", "updated", "failed", "blocked",
}
```

```python
class ResponseArtifact(BaseModel):
    id: str
    title: str
    kind: str

class ActionRecord(BaseModel):
    verb: str            # one of ACTION_VERBS — runtime-owned
    description: str
    target: str | None = None

class StructuredResponse(BaseModel):
    response_type: str               # discriminator; one of RESPONSE_TYPES
    status: str                      # runtime-owned; one of STATUSES
    summary: str                     # model prose — the outcome + body
    key_findings: list[str] = Field(default_factory=list)   # model prose
    assumptions: list[str] = Field(default_factory=list)     # model prose
    risks: list[str] = Field(default_factory=list)           # model prose
    next_action: str | None = None   # model prose
    confidence: str | None = None    # model prose; "high"|"medium"|"low"
    artifacts: list[ResponseArtifact] = Field(default_factory=list)  # runtime
    actions: list[ActionRecord] = Field(default_factory=list)        # runtime
    approval_status: str | None = None  # runtime: None|"drafted_not_sent"|"awaiting"|"approved"
```

**Field ownership (the truth contract — enforced by `apply_truth_guard`):**

| Field | Owner | Source |
|---|---|---|
| `status` | runtime | `task.status` (or `complete` for fast path) |
| `approval_status` | runtime | approval-record existence for the task |
| `artifacts` | runtime | artifacts produced this run (`result["artifacts"]` / task) |
| `actions` (verbs) | runtime | tool_result presence in the loop history |
| `confidence` (capped) | model, capped by runtime | `low` if no sources/tools used |
| `summary`, `key_findings`, `assumptions`, `risks`, `next_action` | model | `complete_json` prose call |

---

### Task 1: Add the `structured_response` column (persistence first)

**Files:**
- Create: `apps/api/migrations/versions/0023_message_structured_response.py`
- Test: `apps/api/tests/test_structured_response.py`

- [ ] **Step 1: Write the failing test**

```python
# apps/api/tests/test_structured_response.py
import pytest
from sqlalchemy import insert, select
from core.config import settings


@pytest.mark.asyncio
async def test_messages_table_has_structured_response_column():
    from core.db import engine, reflect_table

    messages = await reflect_table("messages")
    assert "structured_response" in messages.c, (
        "structured_response column missing — run alembic upgrade head"
    )

    conversations = await reflect_table("conversations")
    async with engine.begin() as conn:
        conv_id = (
            await conn.execute(
                insert(conversations)
                .values(organization_id=settings.org_id, region=settings.region,
                        member_id="member-1", title="t")
                .returning(conversations.c.id)
            )
        ).scalar_one()
        envelope = {"response_type": "direct_answer", "status": "complete", "summary": "hi"}
        await conn.execute(
            insert(messages).values(
                organization_id=settings.org_id, region=settings.region,
                conversation_id=conv_id, role="assistant", content="hi",
                structured_response=envelope,
            )
        )
        row = (
            await conn.execute(
                select(messages.c.structured_response).where(messages.c.conversation_id == conv_id)
            )
        ).scalar_one()
    assert row == envelope
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/api && pytest tests/test_structured_response.py::test_messages_table_has_structured_response_column -v`
Expected: FAIL — `assert "structured_response" in messages.c` raises (column missing).

- [ ] **Step 3: Confirm there is exactly one Alembic head**

The migration history has duplicate numeric prefixes (two `0018`, two `0019`). Before writing a new migration, confirm the chain has a single tip:

Run: `cd apps/api && alembic heads`
Expected: exactly one head — `0022_artifact_workspace`.
- If it prints exactly one head, proceed with `down_revision = "0022_artifact_workspace"` as written below.
- If it prints **multiple heads**, do NOT use `0022` blindly. Either set `down_revision` to the true single head, or create a merge revision (`alembic merge -m "merge heads" <head1> <head2>`) and base the new migration on that merge. `alembic upgrade head` will fail with "multiple heads present" until this is resolved.

- [ ] **Step 4: Write the migration**

```python
# apps/api/migrations/versions/0023_message_structured_response.py
"""message structured_response — canonical structured envelope per assistant message

Revision ID: 0023_message_structured_response
Revises: 0022_artifact_workspace
Create Date: 2026-06-01
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0023_message_structured_response"
down_revision = "0022_artifact_workspace"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "messages",
        sa.Column("structured_response", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("messages", "structured_response")
```

- [ ] **Step 5: Apply the migration and run the test**

Run: `cd apps/api && alembic upgrade head && pytest tests/test_structured_response.py::test_messages_table_has_structured_response_column -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/api/migrations/versions/0023_message_structured_response.py apps/api/tests/test_structured_response.py
git commit -m "feat(messages): add structured_response JSONB column"
```

---

### Task 2: Define the envelope model + serialization

**Files:**
- Create: `apps/api/core/structured_response.py`
- Test: `apps/api/tests/test_structured_response.py` (append)

- [ ] **Step 1: Write the failing test**

```python
# append to apps/api/tests/test_structured_response.py

def test_envelope_serializes_with_defaults():
    from core.structured_response import StructuredResponse

    env = StructuredResponse(response_type="direct_answer", status="complete", summary="Paris.")
    dumped = env.model_dump()
    assert dumped["response_type"] == "direct_answer"
    assert dumped["status"] == "complete"
    assert dumped["key_findings"] == []
    assert dumped["artifacts"] == []
    assert dumped["actions"] == []
    assert dumped["approval_status"] is None


def test_envelope_rejects_unknown_status():
    import pytest as _pytest
    from core.structured_response import StructuredResponse

    with _pytest.raises(ValueError):
        StructuredResponse(response_type="task_complete", status="banana", summary="x")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/api && pytest tests/test_structured_response.py::test_envelope_serializes_with_defaults tests/test_structured_response.py::test_envelope_rejects_unknown_status -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'core.structured_response'`.

- [ ] **Step 3: Write the model**

```python
# apps/api/core/structured_response.py
"""Canonical structured-response envelope for Chronos chat.

The runtime owns truth fields (status, approval_status, artifacts, action verbs);
the model owns prose fields (summary, findings, assumptions, risks, next_action).
See docs/superpowers/plans/2026-06-01-structured-response-spine.md for the contract.
"""
from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

RESPONSE_TYPES = {"direct_answer", "task_complete"}
STATUSES = {
    "complete", "in_progress", "needs_input", "needs_approval",
    "partial", "blocked", "failed", "cancelled",
}
ACTION_VERBS = {
    "suggested", "drafted", "prepared", "scheduled",
    "sent", "updated", "failed", "blocked",
}
CONFIDENCE_LEVELS = {"high", "medium", "low"}


class ResponseArtifact(BaseModel):
    id: str
    title: str
    kind: str


class ActionRecord(BaseModel):
    verb: str
    description: str
    target: str | None = None

    @field_validator("verb")
    @classmethod
    def _verb_known(cls, v: str) -> str:
        if v not in ACTION_VERBS:
            raise ValueError(f"unknown action verb: {v}")
        return v


class StructuredResponse(BaseModel):
    response_type: str
    status: str
    summary: str
    key_findings: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    next_action: str | None = None
    confidence: str | None = None
    artifacts: list[ResponseArtifact] = Field(default_factory=list)
    actions: list[ActionRecord] = Field(default_factory=list)
    approval_status: str | None = None

    @field_validator("response_type")
    @classmethod
    def _type_known(cls, v: str) -> str:
        if v not in RESPONSE_TYPES:
            raise ValueError(f"unknown response_type: {v}")
        return v

    @field_validator("status")
    @classmethod
    def _status_known(cls, v: str) -> str:
        if v not in STATUSES:
            raise ValueError(f"unknown status: {v}")
        return v

    @field_validator("confidence")
    @classmethod
    def _confidence_known(cls, v: str | None) -> str | None:
        if v is not None and v not in CONFIDENCE_LEVELS:
            raise ValueError(f"unknown confidence: {v}")
        return v
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/api && pytest tests/test_structured_response.py::test_envelope_serializes_with_defaults tests/test_structured_response.py::test_envelope_rejects_unknown_status -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/api/core/structured_response.py apps/api/tests/test_structured_response.py
git commit -m "feat(core): add StructuredResponse envelope model"
```

---

### Task 3: Runtime-fact extraction + truth guard

Derive truth fields from a task's `result` dict + loop history. This is where the doctrine's "runtime decides, not the model" is enforced.

**Files:**
- Modify: `apps/api/core/structured_response.py`
- Test: `apps/api/tests/test_structured_response.py` (append)

- [ ] **Step 1: Write the failing test**

```python
# append to apps/api/tests/test_structured_response.py

def test_build_runtime_facts_extracts_artifacts_and_status():
    from core.structured_response import build_runtime_facts

    facts = build_runtime_facts(
        result={"answer": "done", "artifacts": [
            {"id": "a1", "title": "Risk Memo", "kind": "memo"}]},
        task_status="complete",
        tool_summaries=["browser__search done", "gmail.draft created draft"],
        approval_exists=True,
    )
    assert facts.status == "complete"
    assert [a.id for a in facts.artifacts] == ["a1"]
    # gmail.draft → "drafted" verb; never "sent" without a send tool-result
    assert any(a.verb == "drafted" for a in facts.actions)
    assert all(a.verb != "sent" for a in facts.actions)
    assert facts.approval_status == "drafted_not_sent"


def test_derive_response_type_distinguishes_answer_from_work():
    from core.structured_response import build_runtime_facts, derive_response_type

    # No tools, no artifacts, no approval → plain answer even via the loop.
    plain = build_runtime_facts(result={"answer": "Paris."}, task_status="complete",
                                tool_summaries=[], approval_exists=False)
    assert derive_response_type(plain) == "direct_answer"

    # Used a tool → real work.
    worked = build_runtime_facts(result={"answer": "done"}, task_status="complete",
                                 tool_summaries=["browser__search: done"], approval_exists=False)
    assert derive_response_type(worked) == "task_complete"


def test_truth_guard_blocks_unverified_send():
    from core.structured_response import build_runtime_facts, apply_truth_guard, StructuredResponse

    facts = build_runtime_facts(
        result={"answer": "I sent the email."},
        task_status="complete",
        tool_summaries=["gmail.draft created draft"],   # NO send result
        approval_exists=False,
    )
    # Model prose claims it was sent; runtime has no send action.
    env = StructuredResponse(
        response_type="task_complete", status="complete",
        summary="I sent the email to the client.",
    )
    guarded = apply_truth_guard(env, facts)
    # No "sent" action is fabricated; an advisory is appended to assumptions.
    assert all(a.verb != "sent" for a in guarded.actions)
    assert any("not sent" in r.lower() or "draft" in r.lower() for r in guarded.risks)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/api && pytest tests/test_structured_response.py -k "runtime_facts or truth_guard or derive_response_type" -v`
Expected: FAIL — `ImportError: cannot import name 'build_runtime_facts'`.

- [ ] **Step 3: Implement extraction + guard**

```python
# append to apps/api/core/structured_response.py

class RuntimeFacts(BaseModel):
    """Truth fields derived from the runtime, never from model prose."""
    status: str
    artifacts: list[ResponseArtifact] = Field(default_factory=list)
    actions: list[ActionRecord] = Field(default_factory=list)
    approval_status: str | None = None
    used_tools: bool = False


# Map a tool name fragment → the verb its successful result proves.
_VERB_BY_TOOL_FRAGMENT = {
    "gmail.send": "sent",
    "gmail.draft": "drafted",
    "draft": "drafted",
    "schedule": "scheduled",
    "calendar": "scheduled",
    "update": "updated",
    "crm": "updated",
}


def _verb_for_summary(summary: str) -> str | None:
    low = summary.lower()
    if "fail" in low or "error" in low:
        return "failed"
    for fragment, verb in _VERB_BY_TOOL_FRAGMENT.items():
        if fragment in low:
            return verb
    return None


def build_runtime_facts(
    *,
    result: dict,
    task_status: str,
    tool_summaries: list[str],
    approval_exists: bool,
) -> RuntimeFacts:
    """Derive runtime-owned truth fields. Action verbs come ONLY from tool results."""
    artifacts = [
        ResponseArtifact(id=str(a["id"]), title=str(a.get("title", "artifact")),
                         kind=str(a.get("kind", "file")))
        for a in (result.get("artifacts") or [])
        if isinstance(a, dict) and a.get("id")
    ]
    actions: list[ActionRecord] = []
    for summary in tool_summaries:
        verb = _verb_for_summary(summary)
        if verb:
            actions.append(ActionRecord(verb=verb, description=summary))

    has_draft = any(a.verb == "drafted" for a in actions)
    has_sent = any(a.verb == "sent" for a in actions)
    if has_sent:
        approval_status = "approved"
    elif has_draft or approval_exists:
        approval_status = "drafted_not_sent"
    else:
        approval_status = None

    status = task_status if task_status in STATUSES else "complete"
    return RuntimeFacts(
        status=status, artifacts=artifacts, actions=actions,
        approval_status=approval_status, used_tools=bool(tool_summaries),
    )


def derive_response_type(facts: RuntimeFacts) -> str:
    """Pick the response_type from the work performed, not the code path.

    A loop run that used no tools, produced no artifacts, and has no approval is
    just a direct answer (e.g. a factual question routed through the loop because
    it contained a '?'). Only real work earns the task_complete card treatment.
    """
    if facts.used_tools or facts.artifacts or facts.approval_status:
        return "task_complete"
    return "direct_answer"


def apply_truth_guard(env: StructuredResponse, facts: RuntimeFacts) -> StructuredResponse:
    """Overwrite truth fields with runtime values; downgrade unverified side-effect claims."""
    env.status = facts.status
    env.artifacts = facts.artifacts
    env.actions = facts.actions
    env.approval_status = facts.approval_status

    # Guard: prose claims a send the runtime never performed.
    prose = env.summary.lower()
    claims_send = any(w in prose for w in ("i sent", "i've sent", "email sent", "has been sent"))
    if claims_send and all(a.verb != "sent" for a in facts.actions):
        env.risks.append(
            "Draft prepared but not sent — sending requires an approval before it goes out."
        )

    # Cap confidence: no tools/sources used → cannot be 'high'.
    if not facts.used_tools and env.confidence == "high":
        env.confidence = "medium"
    return env
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/api && pytest tests/test_structured_response.py -k "runtime_facts or truth_guard or derive_response_type" -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/api/core/structured_response.py apps/api/tests/test_structured_response.py
git commit -m "feat(core): runtime-fact extraction and truth guard for responses"
```

---

### Task 4: The prose composer (`compose`)

One `complete_json` call produces prose fields; the runtime overlays truth fields via the guard. Respects the customization verbosity setting.

**Files:**
- Modify: `apps/api/core/structured_response.py`
- Test: `apps/api/tests/test_structured_response.py` (append)

- [ ] **Step 1: Write the failing test**

```python
# append to apps/api/tests/test_structured_response.py

@pytest.mark.asyncio
async def test_compose_merges_prose_and_truth(monkeypatch):
    import json as _json
    from core import structured_response as sr

    async def fake_complete_json(prompt, *, model=None):
        return _json.dumps({
            "summary": "Contract review complete. Indemnity is the main risk.",
            "key_findings": ["Uncapped indemnity", "Weak termination rights"],
            "assumptions": ["Reviewed as a general commercial agreement"],
            "risks": ["Jurisdiction-specific enforceability not verified"],
            "next_action": "Review the memo and approve the email draft.",
            "confidence": "medium",
        })

    monkeypatch.setattr(sr, "complete_json", fake_complete_json)

    facts = sr.build_runtime_facts(
        result={"answer": "done", "artifacts": [{"id": "a1", "title": "Risk Memo", "kind": "memo"}]},
        task_status="complete",
        tool_summaries=["gmail.draft created draft"],
        approval_exists=True,
    )
    env = await sr.compose(
        response_type="task_complete",
        answer_text="Contract review complete...",
        facts=facts,
        verbosity="detailed",
    )
    assert env.response_type == "task_complete"
    assert env.status == "complete"                       # runtime
    assert env.approval_status == "drafted_not_sent"      # runtime
    assert [a.id for a in env.artifacts] == ["a1"]        # runtime
    assert "Uncapped indemnity" in env.key_findings       # model prose
    assert env.next_action and "approve" in env.next_action.lower()


@pytest.mark.asyncio
async def test_compose_concise_drops_secondary_sections(monkeypatch):
    import json as _json
    from core import structured_response as sr

    async def fake_complete_json(prompt, *, model=None):
        return _json.dumps({
            "summary": "Done.", "key_findings": ["x"], "assumptions": ["y"],
            "risks": ["z"], "next_action": "Do the thing.", "confidence": "high",
        })

    monkeypatch.setattr(sr, "complete_json", fake_complete_json)
    facts = sr.build_runtime_facts(result={"answer": "done"}, task_status="complete",
                                   tool_summaries=[], approval_exists=False)
    env = await sr.compose(response_type="direct_answer", answer_text="Done.",
                           facts=facts, verbosity="concise")
    # concise: keep summary + next_action, drop findings/assumptions/risks
    assert env.summary == "Done."
    assert env.key_findings == []
    assert env.assumptions == []
    assert env.risks == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/api && pytest tests/test_structured_response.py -k "compose" -v`
Expected: FAIL — `AttributeError: module 'core.structured_response' has no attribute 'compose'`.

- [ ] **Step 3: Implement the composer**

```python
# add near the top of apps/api/core/structured_response.py, with the other imports
import json
from core.llm import complete_json
```

```python
# append to apps/api/core/structured_response.py

_COMPOSE_PROMPT = """You are formatting an assistant answer into structured fields for an \
enterprise work UI. Do NOT invent actions, sends, or approvals — those are filled by the \
runtime, not you. Only describe what the answer text supports.

Return JSON with exactly these keys:
- "summary": one or two sentences leading with the outcome. No "Here are the results".
- "key_findings": list of short strings (the most important results). [] if none.
- "assumptions": list of short strings (assumptions that materially affect the output). [] if none.
- "risks": list of short strings (risks / caveats for high-stakes work). [] if none.
- "next_action": one concrete next step as a string, or null.
- "confidence": "high", "medium", or "low".

ANSWER TEXT:
{answer}
"""


async def compose(
    *,
    response_type: str,
    answer_text: str,
    facts: RuntimeFacts,
    verbosity: str = "detailed",
) -> StructuredResponse:
    """Produce a StructuredResponse: model prose + runtime truth, guarded.

    `verbosity` is "concise" or "detailed" (from the response_format setting).
    On any LLM/parse failure, fall back to a minimal envelope wrapping the raw answer.
    """
    try:
        raw = await complete_json(_COMPOSE_PROMPT.format(answer=answer_text[:6000]))
        prose = json.loads(raw)
    except Exception:
        prose = {"summary": answer_text.strip() or "Done.", "key_findings": [],
                 "assumptions": [], "risks": [], "next_action": None, "confidence": None}

    def _strlist(key: str) -> list[str]:
        val = prose.get(key)
        return [str(x) for x in val] if isinstance(val, list) else []

    env = StructuredResponse(
        response_type=response_type,
        status=facts.status,
        summary=str(prose.get("summary") or answer_text.strip() or "Done."),
        key_findings=_strlist("key_findings"),
        assumptions=_strlist("assumptions"),
        risks=_strlist("risks"),
        next_action=(str(prose["next_action"]) if prose.get("next_action") else None),
        confidence=(prose.get("confidence") if prose.get("confidence") in CONFIDENCE_LEVELS else None),
    )

    if verbosity == "concise":
        env.key_findings = []
        env.assumptions = []
        env.risks = []

    return apply_truth_guard(env, facts)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/api && pytest tests/test_structured_response.py -k "compose" -v`
Expected: PASS (the guard may add a risk in `concise` only when a send is claimed; the test's summary "Done." claims none, so `risks` stays `[]`).

- [ ] **Step 5: Commit**

```bash
git add apps/api/core/structured_response.py apps/api/tests/test_structured_response.py
git commit -m "feat(core): prose composer merging model output with runtime truth"
```

---

### Task 5: Read the `response_format` customization setting

The doctrine requires user-customizable experience. Scope: a verbosity toggle + section-visibility, stored in the existing settings system (no new table, no signature changes).

**Files:**
- Modify: `apps/api/core/structured_response.py`
- Test: `apps/api/tests/test_structured_response.py` (append)

- [ ] **Step 1: Write the failing test**

```python
# append to apps/api/tests/test_structured_response.py

@pytest.mark.asyncio
async def test_resolve_verbosity_defaults_to_detailed(monkeypatch):
    from core import structured_response as sr
    from core.models import RequesterContext

    async def fake_get_settings_doc(member, section, *, scope=None, scope_id=None):
        return {}  # nothing saved

    monkeypatch.setattr(sr, "get_settings_doc", fake_get_settings_doc)
    ctx = RequesterContext(org_id="default", member_id="member-1", role="user")
    assert await sr.resolve_verbosity(ctx) == "detailed"


@pytest.mark.asyncio
async def test_resolve_verbosity_reads_saved_setting(monkeypatch):
    from core import structured_response as sr
    from core.models import RequesterContext

    async def fake_get_settings_doc(member, section, *, scope=None, scope_id=None):
        assert section == "response_format"
        return {"verbosity": "concise"}

    monkeypatch.setattr(sr, "get_settings_doc", fake_get_settings_doc)
    ctx = RequesterContext(org_id="default", member_id="member-1", role="user")
    assert await sr.resolve_verbosity(ctx) == "concise"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/api && pytest tests/test_structured_response.py -k "resolve_verbosity" -v`
Expected: FAIL — `AttributeError: ... has no attribute 'resolve_verbosity'`.

- [ ] **Step 3: Implement the resolver**

`get_settings_doc` takes a `Member` (see `apps/api/core/settings_store.py:132`). Build a lightweight member shim from the requester context — only `id`, `organization_id`, `role` are read by `get_settings_doc`.

```python
# add to the imports block in apps/api/core/structured_response.py
from core.settings_store import get_settings_doc
from core.models import Member, RequesterContext
```

```python
# append to apps/api/core/structured_response.py

_VALID_VERBOSITY = {"concise", "detailed"}


async def resolve_verbosity(ctx: RequesterContext) -> str:
    """Read the member's response_format.verbosity setting. Defaults to 'detailed'."""
    member = Member(id=ctx.member_id, organization_id=ctx.org_id,
                    email="", role=ctx.role, name=None)
    try:
        doc = await get_settings_doc(member, "response_format")
    except Exception:
        return "detailed"
    v = (doc or {}).get("verbosity")
    return v if v in _VALID_VERBOSITY else "detailed"
```

> If `Member(...)` raises due to required fields, open `apps/api/core/models.py`, read the `Member` model, and supply the minimal valid kwargs. Do not change the `Member` model.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/api && pytest tests/test_structured_response.py -k "resolve_verbosity" -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/api/core/structured_response.py apps/api/tests/test_structured_response.py
git commit -m "feat(core): resolve response_format verbosity setting"
```

---

### Task 6: Wire the composer into the agent loop final answer (`task_complete`)

This is the gold-standard path: `run_loop` produces the final answer and persists it. Build + persist + emit the envelope here.

**Files:**
- Modify: `apps/api/runtime/agent_loop.py` (final-answer block, around lines 1148–1166; `_persist_to_conversation` around line 195)
- Test: `apps/api/tests/test_structured_response.py` (append)

- [ ] **Step 1: Read the exact current code**

Read `apps/api/runtime/agent_loop.py:1148-1166` (the `if not calls:` final-answer block) and `apps/api/runtime/agent_loop.py:195-260` (`_persist_to_conversation` / `_save_assistant_message`). Confirm `_save_assistant_message` is where the `messages` row is inserted, and note how it obtains `conversation_id` and the requester/task. The composer call is added in the final-answer block; the persisted envelope is passed down to the insert.

- [ ] **Step 2: Write the failing test**

```python
# append to apps/api/tests/test_structured_response.py

@pytest.mark.asyncio
async def test_collect_tool_summaries_from_history():
    from runtime.agent_loop import collect_tool_summaries

    history = [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]},
        {"role": "tool", "tool_call_id": "c1", "name": "gmail.draft",
         "content": '{"summary": "created draft to client"}'},
        {"role": "assistant", "content": "Drafted the email."},
    ]
    summaries = collect_tool_summaries(history)
    assert any("gmail.draft" in s or "draft" in s.lower() for s in summaries)
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd apps/api && pytest tests/test_structured_response.py::test_collect_tool_summaries_from_history -v`
Expected: FAIL — `ImportError: cannot import name 'collect_tool_summaries'`.

- [ ] **Step 4: Add the history helper**

```python
# add to apps/api/runtime/agent_loop.py (module level, near the other helpers ~line 488)
import json as _json_for_summaries

def collect_tool_summaries(history: list[dict[str, Any]]) -> list[str]:
    """Extract one summary string per tool result in the loop history.

    Used to derive runtime action verbs (drafted/sent/updated) for the
    structured response. Reads the 'summary' field the broker writes into
    tool messages; falls back to the tool name.
    """
    out: list[str] = []
    for msg in history:
        if msg.get("role") != "tool":
            continue
        name = str(msg.get("name") or "tool")
        summary = ""
        content = msg.get("content")
        if isinstance(content, str):
            try:
                summary = str(_json_for_summaries.loads(content).get("summary") or "")
            except Exception:
                summary = ""
        out.append(f"{name}: {summary}".strip())
    return out
```

- [ ] **Step 5: Run the helper test**

Run: `cd apps/api && pytest tests/test_structured_response.py::test_collect_tool_summaries_from_history -v`
Expected: PASS.

- [ ] **Step 6: Build + persist the envelope in the final-answer block**

In the `if not calls:` block (the one that sets `result = {"answer": final_text or ""}` and calls `_persist_to_conversation(task, format_task_answer(result))`), add the envelope build immediately before persistence:

```python
# inside run_loop's final-answer block, after `result = {"answer": final_text or ""}`
from core.structured_response import (
    build_runtime_facts, compose, resolve_verbosity, derive_response_type,
)
from core.models import RequesterContext

answer_text = format_task_answer(result)
try:
    approvals = await reflect_table("approvals")
    async with engine.begin() as conn:
        approval_exists = (
            await conn.execute(
                select(approvals.c.id).where(approvals.c.task_id == task_id).limit(1)
            )
        ).first() is not None
    facts = build_runtime_facts(
        result=result,
        task_status="complete",
        tool_summaries=collect_tool_summaries(history),
        approval_exists=approval_exists,
    )
    triggered_member = task.get("triggered_by_member_id") or "system"
    verbosity = await resolve_verbosity(
        RequesterContext(org_id=task.get("organization_id", "default"),
                         member_id=str(triggered_member), role="user")
    )
    # Derive the type from the WORK, not the code path. A no-tool, no-artifact,
    # no-approval answer that happens to route through the loop (e.g. a plain
    # question with a "?") is a direct_answer — it must NOT get the task_complete
    # card treatment. This avoids the doctrine's core "rigid template" failure.
    envelope = await compose(
        response_type=derive_response_type(facts), answer_text=answer_text,
        facts=facts, verbosity=verbosity,
    )
    envelope_dict = envelope.model_dump()
except Exception:
    envelope_dict = None

message_id = await _persist_to_conversation(task, answer_text, structured_response=envelope_dict)
event: dict[str, Any] = {"type": "task_complete", "result": result}
if envelope_dict is not None:
    event["structured_response"] = envelope_dict
if message_id:
    event["message_id"] = message_id
await emit_activity(task_id, event)
return result
```

> Use the existing `engine`, `reflect_table`, `select` imports already present at the top of `agent_loop.py`. If `select` is not imported, add `from sqlalchemy import select`.

- [ ] **Step 7: Thread `structured_response` through persistence**

Update `_persist_to_conversation` and `_save_assistant_message` (around lines 195 and 211) to accept and store the envelope. Add the parameter and pass it into the `insert(messages).values(...)`:

```python
# _persist_to_conversation signature
async def _persist_to_conversation(
    task: dict[str, Any], content: str, *, structured_response: dict | None = None
) -> str | None:
    ...
    # forward to _save_assistant_message:
    return await _save_assistant_message(
        conversation_id, content, task, mode=task.get("mode"),
        structured_response=structured_response,
    )
```

```python
# _save_assistant_message signature + insert
async def _save_assistant_message(
    conversation_id: str, content: str, task: dict[str, Any], *,
    mode: str | None = None, structured_response: dict | None = None,
) -> str | None:
    ...
    await conn.execute(
        insert(messages).values(
            organization_id=...,  # keep existing values
            region=...,
            conversation_id=conversation_id,
            role="assistant",
            content=content,
            structured_response=structured_response,  # ADD this line
            # ... keep all other existing columns unchanged
        ).returning(messages.c.id)
    )
```

> Read the current body of `_save_assistant_message` first and add only the `structured_response=structured_response` line to the existing `.values(...)`. Do not alter the other columns.

- [ ] **Step 8: Write the persistence round-trip test**

```python
# append to apps/api/tests/test_structured_response.py

@pytest.mark.asyncio
async def test_persist_to_conversation_stores_envelope(monkeypatch):
    from sqlalchemy import insert, select
    from core.config import settings
    from core.db import engine, reflect_table
    from runtime import agent_loop

    conversations = await reflect_table("conversations")
    messages = await reflect_table("messages")
    async with engine.begin() as conn:
        conv_id = (
            await conn.execute(
                insert(conversations).values(
                    organization_id=settings.org_id, region=settings.region,
                    member_id="member-1", title="t",
                ).returning(conversations.c.id)
            )
        ).scalar_one()

    task = {"id": "task-1", "organization_id": settings.org_id, "mode": None}
    # _conversation_id_for must resolve to conv_id — patch it for the unit test.
    monkeypatch.setattr(agent_loop, "_conversation_id_for", lambda t: str(conv_id))

    envelope = {"response_type": "task_complete", "status": "complete", "summary": "done"}
    await agent_loop._persist_to_conversation(task, "done", structured_response=envelope)

    async with engine.begin() as conn:
        row = (
            await conn.execute(
                select(messages.c.structured_response).where(
                    messages.c.conversation_id == conv_id, messages.c.role == "assistant"
                )
            )
        ).scalar_one()
    assert row == envelope
```

> Check `_conversation_id_for` (agent_loop.py:125) for the real resolution; adapt the patch if its name/behavior differs.

- [ ] **Step 9: Run the round-trip + existing loop tests**

Run: `cd apps/api && pytest tests/test_structured_response.py::test_persist_to_conversation_stores_envelope tests/test_chat_turn.py -v`
Expected: PASS for the new test; `test_chat_turn.py` still green (no regressions in the loop).

- [ ] **Step 10: Commit**

```bash
git add apps/api/runtime/agent_loop.py apps/api/tests/test_structured_response.py
git commit -m "feat(runtime): build and persist structured response on task completion"
```

---

### Task 7: Fast-path `direct_answer` envelope + return it from the messages API

The trivial-chat fast path (`stream()` in `chat.py`) must emit a minimal envelope so the frontend has one rendering path. And `GET /conversations/{id}/messages` must return the stored envelope.

**Files:**
- Modify: `apps/api/routers/chat.py` (`_save_message` ~line 64; `stream()` ~line 422; `list_messages` ~line 106)
- Test: `apps/api/tests/test_structured_response.py` (append)

- [ ] **Step 1: Write the failing test**

```python
# append to apps/api/tests/test_structured_response.py

@pytest.mark.asyncio
async def test_save_message_persists_envelope_and_api_returns_it():
    from sqlalchemy import insert, select
    from core.config import settings
    from core.db import engine, reflect_table
    from routers import chat as chat_router

    conversations = await reflect_table("conversations")
    async with engine.begin() as conn:
        conv_id = (
            await conn.execute(
                insert(conversations).values(
                    organization_id=settings.org_id, region=settings.region,
                    member_id="member-1", title="t",
                ).returning(conversations.c.id)
            )
        ).scalar_one()

    envelope = {"response_type": "direct_answer", "status": "complete", "summary": "Paris."}
    await chat_router._save_message(str(conv_id), "assistant", "Paris.",
                                    structured_response=envelope)

    messages = await reflect_table("messages")
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                select(messages).where(messages.c.conversation_id == conv_id,
                                       messages.c.role == "assistant")
            )
        ).mappings().first()
    assert dict(row)["structured_response"] == envelope  # list_messages returns dict(row)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/api && pytest tests/test_structured_response.py::test_save_message_persists_envelope_and_api_returns_it -v`
Expected: FAIL — `_save_message()` got an unexpected keyword argument `structured_response`.

- [ ] **Step 3: Extend `_save_message`**

```python
# apps/api/routers/chat.py — _save_message
async def _save_message(
    conversation_id: str, role: str, content: str, *,
    structured_response: dict | None = None,
) -> None:
    messages = await reflect_table("messages")
    conversations = await reflect_table("conversations")
    async with engine.begin() as conn:
        await conn.execute(
            insert(messages).values(
                organization_id=settings.org_id,
                region=settings.region,
                conversation_id=conversation_id,
                role=role,
                content=content,
                structured_response=structured_response,  # ADD
                token_count=len(content.split()),
            )
        )
        await conn.execute(
            update(conversations)
            .where(conversations.c.id == conversation_id)
            .values(updated_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc))
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/api && pytest tests/test_structured_response.py::test_save_message_persists_envelope_and_api_returns_it -v`
Expected: PASS. (`list_messages` already returns `dict(row)`, so the column flows through automatically — no change needed there.)

- [ ] **Step 5: Emit + persist a `direct_answer` envelope in the fast path**

In `stream()` (chat.py ~line 422), after the answer is fully streamed and `assistant_response` is computed, build and persist the envelope and emit it before `done`:

```python
# apps/api/routers/chat.py — inside stream(), replacing the _save_message + done tail
        assistant_response = full.strip()
        from core.structured_response import build_runtime_facts, compose, resolve_verbosity
        try:
            facts = build_runtime_facts(
                result={"answer": assistant_response}, task_status="complete",
                tool_summaries=[], approval_exists=False,
            )
            verbosity = await resolve_verbosity(requester_context)
            envelope = await compose(
                response_type="direct_answer", answer_text=assistant_response,
                facts=facts, verbosity=verbosity,
            )
            envelope_dict = envelope.model_dump()
        except Exception:
            envelope_dict = None
        await _save_message(conversation_id, "assistant", assistant_response,
                            structured_response=envelope_dict)
        await audit.log("chat_response", member.id, "chat.message", resource_id=conversation_id)
        asyncio.create_task(
            extract_and_save(conversation_id, req.message, assistant_response, requester_context)
        )
        if envelope_dict is not None:
            yield f"data: {json.dumps({'type': 'structured_response', 'structured_response': envelope_dict})}\n\n"
        yield f"data: {json.dumps({'type': 'done'})}\n\n"
```

- [ ] **Step 6: Forward the envelope SSE event in the agent-loop chat stream**

In `_agent_loop_stream` (chat.py ~line 332), the `task_complete` event now carries `structured_response`. Forward it to the browser right before streaming the answer tokens:

```python
# inside _agent_loop_stream, in the event loop where event_type == "task_complete":
            elif event_type == "task_complete":
                final_answer = format_task_answer(event.get("result") or {})
                _sr = event.get("structured_response")
                if _sr is not None:
                    yield f"data: {json.dumps({'type': 'structured_response', 'structured_response': _sr})}\n\n"
                break
```

- [ ] **Step 7: Run the chat router tests**

Run: `cd apps/api && pytest tests/test_chat_turn.py tests/test_chat_routing.py tests/test_rich_messages.py -v`
Expected: PASS (no regressions).

- [ ] **Step 8: Commit**

```bash
git add apps/api/routers/chat.py apps/api/tests/test_structured_response.py
git commit -m "feat(chat): emit and persist structured envelope on both response paths"
```

---

### Task 8: Frontend — type, SSE handling, and load-from-stored

**Files:**
- Modify: `apps/web/app/chat/page.tsx`

- [ ] **Step 1: Add the `StructuredResponse` type and extend `Message`**

After the `ArtifactRef` type (page.tsx ~line 46), add:

```typescript
type ResponseActionRecord = { verb: string; description: string; target?: string | null };
type StructuredResponse = {
  response_type: string;
  status: string;
  summary: string;
  key_findings?: string[];
  assumptions?: string[];
  risks?: string[];
  next_action?: string | null;
  confidence?: string | null;
  artifacts?: ArtifactRef[];
  actions?: ResponseActionRecord[];
  approval_status?: string | null;
};
```

Add `structured_response?: StructuredResponse | null;` to the `Message` type (page.tsx ~line 42, inside the `Message = { ... }` block).

- [ ] **Step 2: Extend the SSE `StreamEvent` type and handle the event**

In `handleStreamEvent`'s local `StreamEvent` type (page.tsx ~line 1254), add `structured_response?: StructuredResponse;`. Then add a branch alongside the others (e.g. after the `artifact` branch ~line 1372):

```typescript
      } else if (ev.type === "structured_response" && ev.structured_response) {
        const sr = ev.structured_response;
        setMessages(prev => {
          const updated = [...prev];
          const last = updated[updated.length - 1];
          if (last?.role !== "assistant") return updated;
          return [...updated.slice(0, -1), { ...last, structured_response: sr }];
        });
```

- [ ] **Step 3: Load the stored envelope when messages come from the server**

Where persisted messages are mapped from the API (page.tsx ~line 1129, where `tool_traces` is read from `m.tool_traces`), add:

```typescript
        structured_response: (m.structured_response as StructuredResponse | undefined) ?? null,
```

- [ ] **Step 4: Pass it into `AssistantMessage`**

At the render site (page.tsx ~line 1572), add the prop:

```tsx
structuredResponse={m.structured_response}
```

And add `structuredResponse?: StructuredResponse | null;` to `AssistantMessage`'s props type and destructured params (page.tsx ~line 2141).

- [ ] **Step 5: Verify the frontend compiles**

Run: `cd apps/web && npx tsc --noEmit`
Expected: No new type errors referencing `structured_response` / `StructuredResponse`.

- [ ] **Step 6: Commit**

```bash
git add apps/web/app/chat/page.tsx
git commit -m "feat(web): plumb structured_response through chat state and SSE"
```

---

### Task 9: Frontend — render the cards

Add the genuinely-new cards and wire them into `AssistantMessage`. Reuse `Tag`, `StatusDot`, `IC`, `ArtifactCard`.

**Files:**
- Modify: `apps/web/app/chat/page.tsx`

- [ ] **Step 1: Add the card components**

Add these functions just above `function AssistantMessage(` (page.tsx ~line 2141):

```tsx
const STATUS_VARIANT: Record<string, "default" | "ok" | "warn" | "danger" | "info" | "accent"> = {
  complete: "ok", in_progress: "accent", needs_input: "warn", needs_approval: "warn",
  partial: "warn", blocked: "danger", failed: "danger", cancelled: "default",
};
const STATUS_LABEL: Record<string, string> = {
  complete: "Complete", in_progress: "In progress", needs_input: "Needs input",
  needs_approval: "Needs approval", partial: "Partially complete", blocked: "Blocked",
  failed: "Failed", cancelled: "Cancelled",
};

function StatusBanner({ sr }: { sr: StructuredResponse }) {
  const variant = STATUS_VARIANT[sr.status] ?? "info";
  return (
    <div className="flex items-center gap-2 mb-2">
      <Tag variant={variant}>{STATUS_LABEL[sr.status] ?? sr.status}</Tag>
      {sr.approval_status === "drafted_not_sent" && <Tag variant="warn">Not sent</Tag>}
      {sr.confidence && <Tag variant="info">{sr.confidence} confidence</Tag>}
    </div>
  );
}

function FindingsRisksCard({ sr }: { sr: StructuredResponse }) {
  const findings = sr.key_findings ?? [];
  const assumptions = sr.assumptions ?? [];
  const risks = sr.risks ?? [];
  if (!findings.length && !assumptions.length && !risks.length) return null;
  return (
    <div className="mt-3 surface border border-soft rounded-lg p-3 space-y-2 text-[13px]">
      {findings.length > 0 && (
        <div>
          <div className="text-[11px] font-medium mb-1" style={{ color: "var(--text-dim)" }}>Key findings</div>
          <ul className="list-disc pl-4 space-y-0.5">{findings.map((f, i) => <li key={i}>{f}</li>)}</ul>
        </div>
      )}
      {assumptions.length > 0 && (
        <div>
          <div className="text-[11px] font-medium mb-1" style={{ color: "var(--text-dim)" }}>Assumptions</div>
          <ul className="list-disc pl-4 space-y-0.5">{assumptions.map((a, i) => <li key={i}>{a}</li>)}</ul>
        </div>
      )}
      {risks.length > 0 && (
        <div>
          <div className="text-[11px] font-medium mb-1" style={{ color: "var(--warn)" }}>Risks & caveats</div>
          <ul className="list-disc pl-4 space-y-0.5" style={{ color: "var(--text-muted)" }}>
            {risks.map((r, i) => <li key={i}>{r}</li>)}
          </ul>
        </div>
      )}
    </div>
  );
}

function InlineApprovalCard({ sr }: { sr: StructuredResponse }) {
  if (sr.approval_status !== "drafted_not_sent") return null;
  return (
    <div className="mt-3 surface border rounded-lg p-3 text-[13px]"
         style={{ borderColor: "var(--warn)" }}>
      <div className="flex items-center gap-2 mb-1">
        <IC.Lock size={13} style={{ color: "var(--warn)" }} />
        <span className="font-medium">Approval required</span>
      </div>
      <div style={{ color: "var(--text-muted)" }}>
        A draft is prepared but not sent. Review and approve it in the Approvals inbox.
      </div>
      <a href="/approvals" className="inline-flex items-center gap-1 mt-2 text-[12.5px]"
         style={{ color: "var(--accent)" }}>
        Open Approvals <IC.ArrowRight size={13} />
      </a>
    </div>
  );
}

function NextActionRow({ sr }: { sr: StructuredResponse }) {
  if (!sr.next_action) return null;
  return (
    <div className="mt-3 flex items-start gap-2 text-[13px]">
      <IC.ArrowRight size={14} style={{ color: "var(--accent)", marginTop: 2 }} />
      <span><span className="font-medium">Next: </span>{sr.next_action}</span>
    </div>
  );
}
```

- [ ] **Step 2: Render them inside `AssistantMessage`**

Inside `AssistantMessage`, destructure `structuredResponse`. **Cards only render for real work** — a `direct_answer` that completed cleanly shows bare prose (no banner, no cards), exactly like a chat reply. Anything that did work (`task_complete`) or did not finish cleanly (any non-`complete` status) gets the operational treatment. Add a guard near the top of the component body:

```tsx
  const sr = structuredResponse ?? null;
  // Plain answers stay plain. Show cards only for real work or unfinished/abnormal state.
  const showCards = !!sr && !isStreaming &&
    (sr.response_type === "task_complete" || sr.status !== "complete");
```

Render `StatusBanner` at the top of the message body (just after the name/header row, before `ReasoningPanel`), and the others after the existing artifacts block (page.tsx ~line 2197):

```tsx
        {showCards && <StatusBanner sr={sr!} />}
        {/* ... existing reasoning, traces, answer, artifacts ... */}
        {showCards && <FindingsRisksCard sr={sr!} />}
        {showCards && <InlineApprovalCard sr={sr!} />}
        {showCards && <NextActionRow sr={sr!} />}
```

> Place `StatusBanner` right after the `<div className="flex items-baseline gap-2 mb-1.5">…</div>` header block. Place the other three immediately after the artifacts `{artifacts && artifacts.length > 0 && (…)}` block.

- [ ] **Step 3: Verify compile + manual round-trip**

Run: `cd apps/web && npx tsc --noEmit`
Expected: clean.

Manual check (document the steps; do not automate here): start the stack (`docker-compose up -d`, API, web), send "Review this and draft an email" with a draft-producing flow, confirm the status banner + "Not sent" + approval card render, then **reload the page** and confirm the cards re-render from stored state.

- [ ] **Step 4: Commit**

```bash
git add apps/web/app/chat/page.tsx
git commit -m "feat(web): render status banner, findings/risks, approval, next-action cards"
```

---

### Task 10: Frontend — customization toggle (verbosity + section visibility)

Read the `response_format` setting and let the user collapse secondary sections. Minimum viable: a single persisted toggle that hides findings/risks/assumptions cards client-side, mirroring the backend `concise` mode.

**Files:**
- Modify: `apps/web/app/chat/page.tsx`

- [ ] **Step 1: Read the setting on mount in `ChatScreen`**

Add state and a fetch in `ChatScreen` (page.tsx ~line 963):

```tsx
  const [responseVerbosity, setResponseVerbosity] = useState<"concise" | "detailed">("detailed");
  useEffect(() => {
    apiFetch("/settings/response_format")
      .then(r => r.json())
      .then((d: { verbosity?: string }) => {
        if (d.verbosity === "concise") setResponseVerbosity("concise");
      })
      .catch(() => { /* default detailed */ });
  }, []);
```

> Confirm the settings GET route shape in `apps/api/routers/settings.py`. If the endpoint is `/settings?section=response_format` or returns `{section: {...}}`, adapt the fetch path and parsing accordingly. Do not invent a route — read `settings.py` first and match it. If no per-section GET exists, add a minimal read using the existing `get_settings_doc` following the patterns already in `settings.py`.

- [ ] **Step 2: Pass verbosity into `AssistantMessage` and gate the secondary cards**

Add `verbosity={responseVerbosity}` to the `AssistantMessage` render (page.tsx ~line 1572) and a `verbosity?: "concise" | "detailed"` prop. Gate `FindingsRisksCard` so concise hides it (building on the `showCards` guard from Task 9):

```tsx
        {showCards && verbosity !== "concise" && <FindingsRisksCard sr={sr!} />}
```

(Keep `StatusBanner`, `InlineApprovalCard`, and `NextActionRow` on the `showCards` guard only — these are operational state, not verbosity-gated.)

- [ ] **Step 3: Verify compile**

Run: `cd apps/web && npx tsc --noEmit`
Expected: clean.

- [ ] **Step 4: Commit**

```bash
git add apps/web/app/chat/page.tsx
git commit -m "feat(web): respect response_format verbosity in chat rendering"
```

---

### Task 11: Full suite + governance check

**Files:** none (verification)

- [ ] **Step 1: Run the backend suite**

Run: `cd apps/api && pytest -x --tb=short`
Expected: all green. Pay attention to `test_governance_invariants.py`, `test_chat_*`, `test_rich_messages.py`, and the new `test_structured_response.py`.

- [ ] **Step 2: Lint + type check**

Run: `cd apps/api && ruff check core/structured_response.py routers/chat.py runtime/agent_loop.py tests/test_structured_response.py && mypy core/structured_response.py`
Expected: clean (fix any issues introduced by this plan only).

- [ ] **Step 3: Frontend type check**

Run: `cd apps/web && npx tsc --noEmit`
Expected: clean.

- [ ] **Step 4: Final commit if any fixes were needed**

```bash
git add -A
git commit -m "chore: lint/type fixes for structured response spine"
```

---

## Self-Review

**1. Spec coverage (doctrine → task):**
- Five questions (result / what Chronos did / what was created / risks / next) → envelope fields `summary`, `actions`, `artifacts`, `risks`+`assumptions`, `next_action` (Tasks 2–4, rendered Task 9). ✅
- Runtime-truth, not model vibes (status/sent/updated decided by runtime) → `build_runtime_facts` + `apply_truth_guard`, action verbs only from tool results (Task 3); guard test blocks unverified "sent". ✅
- **Avoid the "rigid template" failure** (doctrine's core warning) → `derive_response_type` picks `direct_answer` vs `task_complete` from work performed, not the code path; frontend `showCards` guard renders bare prose for a clean `direct_answer` (Tasks 3, 6, 9). ✅
- Status labels (complete/in_progress/needs_approval/blocked/failed/…) → `STATUSES` + `StatusBanner` (Tasks 2, 9). ✅
- Action-truth verbs (suggested/drafted/sent/updated/…) → `ACTION_VERBS` + `ActionRecord` (Tasks 2–3). ✅
- Artifact-first + approval-gated → reuse `ArtifactCard`; `InlineApprovalCard` + "Not sent" tag (Tasks 9). ✅
- Expandable trace = existing `tool_traces` (noted as reused). ✅
- Persists / survives refresh / automated proof (completion rule) → `structured_response` column + round-trip tests (Tasks 1, 6, 7). ✅
- User-customizable experience → `response_format.verbosity` read backend (Task 5) + frontend (Task 10). ✅
- Confidence labels (qualitative, not fake %) → `CONFIDENCE_LEVELS`, capped by runtime (Tasks 2–3). ✅
- **Deferred to Plan 2 (explicitly):** advisory, research, risk_advisory, failure_recovery, clarification, approval_request as *distinct* types and their bespoke cards; admin vs. user trace split; per-vertical formatting. The spine supports them via the `response_type` discriminator.

**2. Placeholder scan:** No "TBD"/"add error handling"/"similar to Task N". Three places say "read the current code first and adapt" (`_save_assistant_message` body, `_conversation_id_for`, settings GET route) — these are deliberate *read-then-match* instructions against existing code the plan author could not fully inline without over-quoting; each names the exact file/line and what to match. Acceptable per "follow established patterns."

**3. Type consistency:** `StructuredResponse`, `ResponseArtifact`, `ActionRecord`, `RuntimeFacts`, `build_runtime_facts`, `derive_response_type`, `apply_truth_guard`, `compose`, `resolve_verbosity`, `collect_tool_summaries` are named identically across backend tasks. Frontend `StructuredResponse`/`ResponseActionRecord` mirror the backend dump shape. SSE event type string is `structured_response` everywhere (backend emit + frontend handler). Settings section is `response_format` in Tasks 5 and 10. ✅

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-06-01-structured-response-spine.md`. Two execution options:

1. **Subagent-Driven (recommended)** — dispatch a fresh subagent per task, review between tasks, fast iteration.
2. **Inline Execution** — execute tasks in this session using executing-plans, batched with checkpoints.

Which approach?
