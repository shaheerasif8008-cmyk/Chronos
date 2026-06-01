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
