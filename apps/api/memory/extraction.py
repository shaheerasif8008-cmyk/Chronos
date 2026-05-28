import json
from datetime import datetime, timedelta, timezone
from typing import Any

from core import audit
from core.config import settings
from core.memory import memory_policy_allows
from core.llm import complete_json
from core.memory_writes import create_memory_entry
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
    requester_context.conversation_id = requester_context.conversation_id or conversation_id
    if not await memory_policy_allows(requester_context, operation="write"):
        await audit.log(
            "memory_extraction_skipped",
            requester_context.member_id,
            "memory.extract",
            resource_type="memory",
            resource_id=conversation_id,
            payload={"reason": "memory_policy_disabled"},
            decision="disabled_by_policy",
        )
        return
    prompt = f"""
Identify facts worth remembering from this exchange.
Return JSON only: {{"memories": [{{"content": "...", "scope": "personal|workspace|persona|org", "importance": 0.0-1.0}}]}}
Only include durable facts. Not conversational filler. If nothing is worth saving, return {{"memories": []}}.

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
            resource_type="memory",
            resource_id=conversation_id,
            payload={"error": str(exc)[:240]},
            decision="skipped",
        )
        return

    for candidate in candidates:
        importance = float(candidate.get("importance", 0))
        content = str(candidate.get("content", "")).strip()
        if importance < 0.6 or not content:
            continue

        scope = candidate.get("scope") or "org"
        scope_id = _scope_id_for(scope, requester_context)
        entry_id = await create_memory_entry(
            content=content,
            requester_context=requester_context,
            source="autonomous",
            scope=scope,
            scope_id=scope_id,
            importance_score=importance,
            conversation_id=conversation_id,
            created_by="chronos",
            confidence_score=float(candidate.get("confidence", 0.75) or 0.75),
            provenance={"conversation_id": conversation_id, "source": "autonomous_extraction"},
        )
        undo_expires = datetime.now(timezone.utc) + timedelta(seconds=60)
        await _redis.publish(
            f"memories:{conversation_id}",
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


def _scope_id_for(scope: str, requester_context: RequesterContext) -> str:
    if scope in {"personal", "restricted"}:
        return requester_context.member_id
    if scope == "workspace":
        return requester_context.workspace_id or requester_context.org_id
    if scope == "project":
        return requester_context.project_id or requester_context.org_id
    if scope == "persona":
        return requester_context.persona_id or requester_context.org_id
    if scope == "conversation":
        return requester_context.conversation_id or requester_context.org_id
    return requester_context.org_id
