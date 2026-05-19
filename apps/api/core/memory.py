from core import audit
from core.models import MemoryEntry, RequesterContext


async def retrieve(query: str, requester_context: RequesterContext) -> list[MemoryEntry]:
    await audit.log(
        "memory_retrieve",
        requester_context.member_id,
        "memory.retrieve",
        resource_type="memory_entries",
        payload={"query_preview": query[:120]},
        decision="unfiltered_stub",
    )
    return []
