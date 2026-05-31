"""Seed an active connector for the default org so the directory shows it as
connected — used by the Connectors UI E2E (apps/web/e2e/connectors.spec.ts).

The directory's real "connect" path is OAuth (can't be driven in E2E), so we
seed the connectors table directly to prove the directory reflects connection
state. Prints:  CONNECTOR_PROVIDER=gmail
"""
from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import delete

from core.config import settings
from core.db import engine, reflect_table

PROVIDER = "gmail"
HANDLE = "e2e@example.com"


async def main() -> None:
    connectors = await reflect_table("connectors")
    async with engine.begin() as conn:
        # Idempotent: drop any prior seeded rows for this provider/org.
        await conn.execute(
            delete(connectors).where(
                connectors.c.organization_id == settings.org_id,
                connectors.c.provider == PROVIDER,
            )
        )
        await conn.execute(
            connectors.insert().values(
                id=str(uuid.uuid4()),
                organization_id=settings.org_id,
                provider=PROVIDER,
                account_handle=HANDLE,
                vault_ref="vault:e2e-seed",
                status="active",
            )
        )
    print(f"CONNECTOR_PROVIDER={PROVIDER}")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
