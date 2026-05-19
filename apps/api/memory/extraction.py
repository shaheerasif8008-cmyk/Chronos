import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import insert

from core import audit
from core.config import settings
from core.db import engine, reflect_table
from core.embeddings import embed
from core.llm import complete_json
from core.models import RequesterContext
from core.redis import redis_client

_redis = redis_client


def _message_content(response: Any) -> str:
    if isinstance(response, dict):
        return response.get("choices", [{}])[0].get("message", {}).get("content") or "{}"
    return response.choices[0].message.content or "{}"


async def _insert_memory_entry(entry: dict) -> str:
    memory_entries = await reflect_table("memory_entries")
    vector_literal = "[" + ",".join(str(value) for value in entry["embedding"]) + "]"
    values = {**entry, "embedding": vector_literal}
    async with engine.begin() as conn:
        result = await conn.execute(
            insert(memory_entries).values(**values).returning(memory_entries.c.id)
        )
        return str(result.scalar_one())


async def extract_and_save(
    conversation_id: str,
    user_message: str,
    assistant_response: str,
    requester_context: RequesterContext,
) -> None:
    prompt = f"""
Identify facts worth remembering from this exchange.
Return JSON only: {{"memories": [{{"content": "...", "scope": "personal|workspace|persona|org", "importance": 0.0-1.0}}]}}
Only include durable facts. Not conversational filler. If nothing is worth saving, return {{"memories": []}}.

User: {user_message}
Assistant: {assistant_response}
"""
    try:
        extraction_json = await complete_json(prompt)
        candidates = json.loads(extraction_json).get("memories", [])
    except json.JSONDecodeError:
        candidates = []
    except Exception as exc:
        await audit.log(
            "memory_extraction_error",
            requester_context.member_id,
            "memory.extract",
            resource_type="memory_entries",
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
        vector = await embed(content)
        entry_id = await _insert_memory_entry(
            {
                "organization_id": requester_context.org_id,
                "region": settings.region,
                "scope": scope,
                "scope_id": requester_context.org_id,
                "content": content,
                "embedding": vector,
                "source": "autonomous",
                "source_conversation_id": conversation_id,
                "importance_score": importance,
                "created_by": "chronos",
            }
        )
        await audit.log(
            "memory_write",
            requester_context.member_id,
            "memory.extract",
            resource_type="memory_entries",
            resource_id=entry_id,
            payload={"source": "autonomous", "scope": scope},
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
