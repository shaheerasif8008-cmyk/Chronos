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
