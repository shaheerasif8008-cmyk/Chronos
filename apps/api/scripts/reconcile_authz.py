"""Backfill OpenFGA relationship tuples from the DB (W2.5).

Reconciles FGA tuples for one org or every org in the database so that
enabling OpenFGA on a populated deployment does not lock out existing members,
projects, or workspaces.

Usage::

    # Reconcile a single org
    python scripts/reconcile_authz.py <org_id>

    # Reconcile every org in the database
    python scripts/reconcile_authz.py

Exit 0 on success, 1 if OpenFGA is not configured (nothing to do).
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_API_ROOT = Path(__file__).resolve().parents[1]
if str(_API_ROOT) not in sys.path:
    sys.path.insert(0, str(_API_ROOT))

from sqlalchemy import select  # noqa: E402

from core import permissions  # noqa: E402
from core.db import engine, reflect_table  # noqa: E402


async def _all_org_ids() -> list[str]:
    orgs = await reflect_table("organizations")
    async with engine.begin() as conn:
        rows = (await conn.execute(select(orgs.c.id))).fetchall()
    return [str(row.id) for row in rows]


async def _run(org_ids: list[str]) -> int:
    if not permissions.settings_openfga_configured():
        print("OpenFGA is not configured — nothing to reconcile.")
        return 1

    total: dict[str, int] = {"members": 0, "projects": 0, "workspaces": 0}
    for org_id in org_ids:
        counts = await permissions.reconcile_org_tuples(org_id)
        print(
            f"org={org_id}  members={counts['members']}"
            f"  projects={counts['projects']}  workspaces={counts['workspaces']}"
        )
        for k in total:
            total[k] += counts[k]

    if len(org_ids) > 1:
        print(
            f"\nTotal: members={total['members']}"
            f"  projects={total['projects']}  workspaces={total['workspaces']}"
        )
    return 0


def main(argv: list[str]) -> int:
    if len(argv) > 1:
        org_ids = [argv[1]]
    else:
        org_ids = asyncio.run(_all_org_ids())
    return asyncio.run(_run(org_ids))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
