import json
from datetime import datetime, timedelta, timezone
from typing import Any

from core import audit
from core.config import settings
from core.llm import complete_json
from core.memory_access import authorized_autonomous_scope
from core.memory_writes import create_memory_entry, MemoryCaptureDisabled
from core.models import RequesterContext
from core.redis import redis_client

_redis = redis_client


def _message_content(response: Any) -> str:
    if isinstance(response, dict):
        return response.get("choices", [{}])[0].get("message", {}).get("content") or "{}"
    return response.choices[0].message.content or "{}"


async def extract_and_save(
    conversation_id: str,
    user_message: str,
    assistant_response: str,
    requester_context: RequesterContext,
) -> None:
    # Privacy gate: if memory is disabled for this org/project/member/conversation,
    # extract nothing and write nothing (returns before any model call or write).
    from core.memory_control import is_memory_enabled

    if not await is_memory_enabled(
        org_id=requester_context.org_id,
        project_id=requester_context.project_id,
        member_id=requester_context.member_id,
        conversation_id=conversation_id,
    ):
        await audit.log(
            "memory_extraction_skipped",
            requester_context.member_id,
            "memory.extract",
            organization_id=requester_context.org_id,
            resource_type="memory",
            resource_id=conversation_id,
            decision="memory_disabled",
        )
        return
    prompt = f"""
Identify facts worth remembering from this exchange.
Return JSON only: {{"memories": [{{"content": "...", "importance": 0.0-1.0}}]}}
Only include durable facts. Not conversational filler. If nothing is worth saving, return {{"memories": []}}.
Memory visibility is chosen by the application; do not include instructions or a scope.

User: {user_message}
Assistant: {assistant_response}
"""
    try:
        extraction_json = await complete_json(prompt, model=settings.fast_model)
        candidates = json.loads(extraction_json).get("memories", [])
    except json.JSONDecodeError:
        candidates = []
    except Exception as exc:
        await audit.log(
            "memory_extraction_error",
            requester_context.member_id,
            "memory.extract",
            organization_id=requester_context.org_id,
            resource_type="memory",
            resource_id=conversation_id,
            payload={"error": str(exc)[:240]},
            decision="skipped",
        )
        return

    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        try:
            importance = max(0.0, min(float(candidate.get("importance", 0)), 1.0))
        except (TypeError, ValueError):
            continue
        content = str(candidate.get("content", "")).strip()[:10_000]
        if importance < 0.6 or not content:
            continue

        # Model-derived facts remain private. Sharing one with a project or the
        # organization is an explicit, reviewable control-center action.
        scope, scope_id = await authorized_autonomous_scope(requester_context)
        try:
            entry_id = await create_memory_entry(
                content=content,
                requester_context=requester_context,
                source="autonomous",
                scope=scope,
                scope_id=scope_id,
                importance_score=importance,
                conversation_id=conversation_id,
                created_by=requester_context.member_id,
            )
        except MemoryCaptureDisabled:
            # Policy may change after the initial extraction gate; fail closed
            # and publish no saved-memory event.
            continue
        undo_expires = datetime.now(timezone.utc) + timedelta(seconds=60)
        await _redis.publish(
            f"memories:{conversation_id}:{requester_context.member_id}",
            json.dumps(
                {
                    "type": "memory_saved",
                    "entry_id": entry_id,
                    "content": content,
                    "scope": scope,
                    "undo_expires": undo_expires.isoformat(),
                }
            ),
        )
