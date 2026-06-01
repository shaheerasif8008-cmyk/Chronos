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
