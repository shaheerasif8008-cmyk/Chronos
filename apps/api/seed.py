import asyncio
from pathlib import Path

from sqlalchemy import insert, select, update

from core.config import settings
from core.db import engine, reflect_table

ROOT = Path(__file__).resolve().parent


async def upsert_seed() -> None:
    organizations = await reflect_table("organizations")
    members = await reflect_table("members")
    async with engine.begin() as conn:
        org = (
            await conn.execute(
                select(organizations.c.id).where(organizations.c.id == settings.org_id)
            )
        ).first()
        if org is None:
            await conn.execute(
                insert(organizations).values(
                    id=settings.org_id,
                    organization_id=settings.org_id,
                    region=settings.region,
                    slug="default",
                    name="Default Chronos Org",
                )
            )

        member = (
            await conn.execute(
                select(members.c.id, members.c.role).where(
                    members.c.organization_id == settings.org_id,
                    members.c.email == settings.admin_email,
                )
            )
        ).mappings().first()
        if member is None:
            await conn.execute(
                insert(members).values(
                    organization_id=settings.org_id,
                    region=settings.region,
                    email=settings.admin_email,
                    role="admin",
                    name="Chronos Admin",
                )
            )
        elif member["role"] not in {"admin", "owner"}:
            await conn.execute(
                update(members)
                .where(members.c.id == member["id"])
                .values(role="admin", region=settings.region)
            )

    # Seed the OpenFGA org-admin tuple for the admin member (no-op unless an
    # OpenFGA server is configured). Admins inherit project access via the model.
    from core import permissions

    async with engine.begin() as conn:
        admin_id = (
            await conn.execute(
                select(members.c.id).where(
                    members.c.organization_id == settings.org_id,
                    members.c.email == settings.admin_email,
                )
            )
        ).scalar_one_or_none()
    if admin_id is not None:
        await permissions.grant_org_membership(str(admin_id), settings.org_id, admin=True)

    context_dir = ROOT / "context" / settings.org_id
    context_dir.mkdir(parents=True, exist_ok=True)
    org_md = context_dir / "org.md"
    if not org_md.exists():
        org_md.write_text(
            "# Default Org\n\n"
            "This is the default local development organization. Do not infer "
            "production account configuration, client identity, or launch readiness "
            "from this context.\n"
        )

    print(f"Seed complete. Admin email: {settings.admin_email}")


if __name__ == "__main__":
    asyncio.run(upsert_seed())
