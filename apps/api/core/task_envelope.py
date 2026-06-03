from __future__ import annotations

import re
from typing import Any

from core.models import TaskEnvelope, TaskEnvelopeUI, TaskExtractedEntities, TaskRouterDecision

_URL_RE = re.compile(r"https?://[^\s<>)\"']+", re.I)
_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
_MONEY_RE = re.compile(r"(?<!\w)\$\s?\d[\d,]*(?:\.\d{1,2})?\b")
_QUOTED_FILE_RE = re.compile(r'"([^"]+\.[A-Za-z0-9]{1,10})"')
_FILE_RE = re.compile(r"\b[\w .\-()]+\.[A-Za-z0-9]{1,10}\b")
_RELATIVE_DATE_RE = re.compile(
    r"\b(?:today|tomorrow|yesterday|tonight|next\s+\w+|last\s+\w+|"
    r"\d{1,2}\s*(?:am|pm)|\d{1,2}:\d{2}\s*(?:am|pm)?|"
    r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2}(?:,\s*\d{4})?)\b",
    re.I,
)


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        cleaned = value.strip().rstrip(".,;:")
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            out.append(cleaned)
    return out


def extract_task_entities(message: str) -> TaskExtractedEntities:
    urls = _dedupe(_URL_RE.findall(message))
    repo_urls = [
        url for url in urls
        if re.search(r"(^https?://)?(www\.)?github\.com/[^/\s]+/[^/\s]+", url, re.I)
    ]
    quoted_files = _QUOTED_FILE_RE.findall(message)
    loose_files = [
        match.strip().strip('"')
        for match in _FILE_RE.findall(message)
        if not match.lower().startswith(("http://", "https://"))
    ]
    return TaskExtractedEntities(
        urls=urls,
        repo_urls=_dedupe(repo_urls),
        emails=_dedupe(_EMAIL_RE.findall(message)),
        file_names=_dedupe(quoted_files + loose_files),
        dates=_dedupe([match.group(0) for match in _RELATIVE_DATE_RE.finditer(message)]),
        money_amounts=_dedupe(_MONEY_RE.findall(message)),
    )


def build_task_envelope(
    *,
    task_id: str,
    raw_user_message: str,
    ui_title: str,
    router_decision: dict[str, Any] | TaskRouterDecision | None = None,
    conversation_context: list[dict[str, Any]] | None = None,
    attachments: list[dict[str, Any]] | None = None,
) -> TaskEnvelope:
    if isinstance(router_decision, TaskRouterDecision):
        decision = router_decision
    else:
        data = dict(router_decision or {})
        if "goal" in data and "ui_title" not in data:
            data["ui_title"] = data.pop("goal")
        decision = TaskRouterDecision(**data)
    if not decision.ui_title:
        decision.ui_title = ui_title
    return TaskEnvelope(
        id=task_id,
        raw_user_message=raw_user_message,
        conversation_context=conversation_context or [],
        attachments=attachments or [],
        extracted_entities=extract_task_entities(raw_user_message),
        router_decision=decision,
        ui=TaskEnvelopeUI(title=ui_title),
    )


def envelope_to_agent_prompt(envelope: TaskEnvelope) -> str:
    entities = envelope.extracted_entities.model_dump()
    decision = envelope.router_decision.model_dump(exclude_none=True)
    conversation_context = [
        {
            "role": msg.get("role"),
            "content": msg.get("content"),
        }
        for msg in envelope.conversation_context
        if isinstance(msg, dict)
    ]
    return (
        "# Task Envelope\n"
        "Use the Original user request as the source of truth. Router metadata is advisory only. "
        "If there is a conflict between the raw user request and router metadata, follow the raw "
        "user request unless higher-priority system/developer policy says otherwise.\n\n"
        f"UI title:\n{envelope.ui.title}\n\n"
        f"Original user request:\n{envelope.raw_user_message}\n\n"
        f"Conversation context:\n{conversation_context}\n\n"
        f"Attachments:\n{envelope.attachments}\n\n"
        f"Extracted entities:\n{entities}\n\n"
        f"Router metadata:\n{decision}\n"
    )
