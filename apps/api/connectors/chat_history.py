from __future__ import annotations

from typing import Any

from sqlalchemy import and_, or_, select

from core.db import engine, reflect_table
from core.models import AgentContext, ToolResult

_DEFAULT_LIMIT = 10
_MAX_LIMIT = 20
_MESSAGES_PER_CONVERSATION = 8
_SNIPPET_LEN = 240


def _escape_like(q: str) -> str:
    return q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _clean_text(value: Any, *, max_len: int = _SNIPPET_LEN) -> str:
    text = str(value or "").strip()
    if len(text) <= max_len:
        return text
    return text[:max_len].rstrip() + "..."


def _limit(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return _DEFAULT_LIMIT
    return max(1, min(parsed, _MAX_LIMIT))


class ChatHistoryConnector:
    async def execute(self, tool: str, args: dict[str, Any], agent: AgentContext) -> ToolResult:
        if tool == "chat_history.recent":
            return await self._recent(args, agent)
        if tool == "chat_history.search":
            return await self._search(args, agent)
        return ToolResult(summary=f"Unknown chat history tool: {tool}", data={"conversations": []})

    async def _recent(self, args: dict[str, Any], agent: AgentContext) -> ToolResult:
        conversations = await reflect_table("conversations")
        limit = _limit(args.get("limit"))
        async with engine.begin() as conn:
            rows = (
                await conn.execute(
                    select(
                        conversations.c.id,
                        conversations.c.title,
                        conversations.c.created_at,
                        conversations.c.updated_at,
                    )
                    .where(
                        and_(
                            conversations.c.organization_id == agent.org_id,
                            conversations.c.member_id == agent.member_id,
                        )
                    )
                    .order_by(conversations.c.updated_at.desc())
                    .limit(limit)
                )
            ).mappings().all()
        enriched = await self._attach_messages(rows, agent)
        return ToolResult(
            summary=f"Retrieved {len(enriched)} recent prior chats.",
            data={"conversations": enriched},
        )

    async def _search(self, args: dict[str, Any], agent: AgentContext) -> ToolResult:
        query = str(args.get("query") or "").strip()
        if not query:
            return ToolResult(summary="No chat history query provided.", data={"conversations": []})
        escaped = _escape_like(query)
        conversations = await reflect_table("conversations")
        messages = await reflect_table("messages")
        limit = _limit(args.get("limit"))
        owned_filter = and_(
            conversations.c.organization_id == agent.org_id,
            conversations.c.member_id == agent.member_id,
        )
        async with engine.begin() as conn:
            rows = (
                await conn.execute(
                    select(
                        conversations.c.id,
                        conversations.c.title,
                        conversations.c.created_at,
                        conversations.c.updated_at,
                    )
                    .select_from(
                        conversations.outerjoin(
                            messages,
                            and_(
                                messages.c.conversation_id == conversations.c.id,
                                messages.c.organization_id == agent.org_id,
                            ),
                        )
                    )
                    .where(
                        and_(
                            owned_filter,
                            or_(
                                conversations.c.title.ilike(f"%{escaped}%", escape="\\"),
                                messages.c.content.ilike(f"%{escaped}%", escape="\\"),
                            ),
                        )
                    )
                    .group_by(
                        conversations.c.id,
                        conversations.c.title,
                        conversations.c.created_at,
                        conversations.c.updated_at,
                    )
                    .order_by(conversations.c.updated_at.desc())
                    .limit(limit)
                )
            ).mappings().all()
        enriched = await self._attach_messages(rows, agent, query=query)
        return ToolResult(
            summary=f"Found {len(enriched)} prior chats matching {query!r}.",
            data={"query": query, "conversations": enriched},
        )

    async def _attach_messages(
        self,
        conversation_rows: list[Any],
        agent: AgentContext,
        *,
        query: str | None = None,
    ) -> list[dict[str, Any]]:
        if not conversation_rows:
            return []
        messages = await reflect_table("messages")
        out: list[dict[str, Any]] = []
        async with engine.begin() as conn:
            for row in conversation_rows:
                conversation_id = str(row["id"])
                msg_rows = (
                    await conn.execute(
                        select(messages.c.id, messages.c.role, messages.c.content, messages.c.created_at)
                        .where(
                            and_(
                                messages.c.organization_id == agent.org_id,
                                messages.c.conversation_id == conversation_id,
                            )
                        )
                        .order_by(messages.c.created_at.asc())
                        .limit(_MESSAGES_PER_CONVERSATION)
                    )
                ).mappings().all()
                excerpts = [
                    {
                        "id": str(msg["id"]),
                        "role": msg["role"],
                        "content": _clean_text(msg["content"]),
                        "created_at": str(msg["created_at"]) if msg["created_at"] is not None else None,
                    }
                    for msg in msg_rows
                ]
                snippet = self._best_snippet(row["title"], excerpts, query=query)
                out.append(
                    {
                        "id": conversation_id,
                        "title": row["title"] or "Untitled",
                        "created_at": str(row["created_at"]) if row["created_at"] is not None else None,
                        "updated_at": str(row["updated_at"]) if row["updated_at"] is not None else None,
                        "url": f"/chat?c={conversation_id}",
                        "snippet": snippet,
                        "messages": excerpts,
                    }
                )
        return out

    def _best_snippet(
        self,
        title: Any,
        excerpts: list[dict[str, Any]],
        *,
        query: str | None,
    ) -> str:
        if query:
            needle = query.lower()
            for excerpt in excerpts:
                content = str(excerpt.get("content") or "")
                if needle in content.lower():
                    return _clean_text(content)
            title_text = str(title or "")
            if needle in title_text.lower():
                return _clean_text(title_text)
        if excerpts:
            return _clean_text(excerpts[0].get("content"))
        return _clean_text(title)


chat_history_connector = ChatHistoryConnector()
