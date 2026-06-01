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
