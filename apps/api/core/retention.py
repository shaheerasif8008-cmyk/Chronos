"""Tenant-safe application data retention and legal-hold enforcement.

The executor deliberately operates only on data classes with an explicit,
user-visible retention policy today:

* active memory entries age into soft deletion;
* soft-deleted memory entries age into irreversible deletion; and
* soft-deleted artifacts age into object-store plus metadata deletion.

Audit records are append-only and are never retention targets.  Pinned memories,
resource holds, and organization-wide holds are excluded from every phase.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import Text, cast, delete, func, literal, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from core import audit
from core.config import settings
from core.db import engine, reflect_table
from core.models import Member
from core.object_storage import delete_object
from core.settings_store import get_settings_doc

log = logging.getLogger(__name__)

HOLD_RESOURCE_TYPES = {"organization", "memory", "artifact", "workspace"}
_LOCAL_ARTIFACT_ROOT = Path("/tmp/chronos_artifacts").resolve()


class RetentionResourceNotFound(Exception):
    """The requested hold target does not exist in the organization."""


class RetentionResourceHeld(Exception):
    """A retention hold blocks the requested deletion."""


@asynccontextmanager
async def _org_retention_lock(org_id: str):
    """Serialize retention and legal-hold mutations for one organization.

    The session-scoped PostgreSQL advisory lock creates a linear order between
    a run and a newly requested hold.  Without it, a hold inserted after the
    executor's first read could lose a race with irreversible deletion.
    """

    lock_key = f"chronos:retention:{org_id}"
    async with engine.connect() as conn:
        await conn.execute(
            text("SELECT pg_advisory_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": lock_key},
        )
        try:
            yield
        finally:
            await conn.execute(
                text("SELECT pg_advisory_unlock(hashtextextended(:lock_key, 0))"),
                {"lock_key": lock_key},
            )


@dataclass(frozen=True)
class RetentionPolicy:
    enabled: bool = True
    configuration_valid: bool = True
    memory_days: int = 365
    deleted_memory_days: int = 30
    deleted_artifact_days: int = 30


@dataclass
class RetentionResult:
    organization_id: str
    dry_run: bool
    retention_enabled: bool
    configuration_valid: bool = True
    organization_hold: bool = False
    memory_soft_delete_candidates: int = 0
    memory_soft_deleted: int = 0
    memory_hard_delete_candidates: int = 0
    memory_hard_deleted: int = 0
    artifact_purge_candidates: int = 0
    artifact_metadata_deleted: int = 0
    artifact_object_delete_candidates: int = 0
    artifact_objects_deleted: int = 0
    artifact_purge_failures: int = 0
    failed_artifact_ids: list[str] = field(default_factory=list)
    held_resources_excluded: int = 0
    pinned_memories_excluded: int = 0
    memory_cutoff: str | None = None
    deleted_memory_cutoff: str | None = None
    deleted_artifact_cutoff: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _validated_days(value: Any, default: int) -> tuple[int, bool]:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default, False
    if not 1 <= parsed <= 3650:
        return default, False
    return parsed, True


async def load_policy(org_id: str) -> RetentionPolicy:
    member = Member(
        id="retention_scheduler",
        organization_id=org_id,
        region=settings.region,
        email="retention@chronos.internal",
        role="admin",
    )
    values = await get_settings_doc(
        member, "memory", scope="org", scope_id=org_id
    )
    memory_days, memory_valid = _validated_days(values.get("retention_days"), 365)
    deleted_memory_days, deleted_memory_valid = _validated_days(
        values.get("deleted_retention_days"), 30
    )
    deleted_artifact_days, deleted_artifact_valid = _validated_days(
        values.get("deleted_artifact_retention_days"), 30
    )
    enabled_value = values.get("retention_enabled", True)
    configuration_valid = (
        isinstance(enabled_value, bool)
        and memory_valid
        and deleted_memory_valid
        and deleted_artifact_valid
    )
    # A malformed legacy settings document must fail closed against deletion.
    return RetentionPolicy(
        enabled=bool(enabled_value) and configuration_valid,
        configuration_valid=configuration_valid,
        memory_days=memory_days,
        deleted_memory_days=deleted_memory_days,
        deleted_artifact_days=deleted_artifact_days,
    )


async def list_holds(
    org_id: str, *, active_only: bool = True
) -> list[dict[str, Any]]:
    holds = await reflect_table("retention_holds")
    conditions = [holds.c.organization_id == org_id]
    if active_only:
        conditions.append(holds.c.released_at.is_(None))
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(holds)
                .where(*conditions)
                .order_by(holds.c.created_at.desc())
            )
        ).mappings().all()
    return [dict(row) for row in rows]


async def resource_exists(org_id: str, resource_type: str, resource_id: str) -> bool:
    if resource_type not in HOLD_RESOURCE_TYPES:
        return False
    if resource_type == "organization":
        return str(resource_id) == str(org_id)
    table_name = {"memory": "memory_entries", "artifact": "artifacts", "workspace": "workspaces"}.get(
        resource_type
    )
    if table_name is None:
        return False
    table = await reflect_table(table_name)
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                select(table.c.id).where(
                    table.c.organization_id == org_id,
                    cast(table.c.id, Text) == str(resource_id),
                )
            )
        ).first()
    return row is not None


async def _create_hold_unlocked(
    *,
    org_id: str,
    region: str,
    resource_type: str,
    resource_id: str,
    reason: str,
    actor_id: str,
) -> dict[str, Any]:
    """Create an active legal hold, idempotently under concurrent requests."""

    holds = await reflect_table("retention_holds")
    statement = (
        pg_insert(holds)
        .values(
            organization_id=org_id,
            region=region,
            resource_type=resource_type,
            resource_id=str(resource_id),
            reason=reason,
            created_by=actor_id,
        )
        .on_conflict_do_nothing(
            index_elements=[
                holds.c.organization_id,
                holds.c.resource_type,
                holds.c.resource_id,
            ],
            index_where=holds.c.released_at.is_(None),
        )
        .returning(holds)
    )
    async with engine.begin() as conn:
        row = (await conn.execute(statement)).mappings().first()
        if row is None:
            row = (
                await conn.execute(
                    select(holds).where(
                        holds.c.organization_id == org_id,
                        holds.c.resource_type == resource_type,
                        holds.c.resource_id == str(resource_id),
                        holds.c.released_at.is_(None),
                    )
                )
            ).mappings().one()
    result = dict(row)
    await audit.log(
        "retention_hold",
        actor_id,
        "retention.hold.create",
        organization_id=org_id,
        resource_type=resource_type,
        resource_id=str(resource_id),
        payload={"hold_id": str(result["id"]), "reason": str(result["reason"])},
        decision="held",
    )
    return result


async def create_hold(
    *,
    org_id: str,
    region: str,
    resource_type: str,
    resource_id: str,
    reason: str,
    actor_id: str,
) -> dict[str, Any]:
    async with _org_retention_lock(org_id):
        if not await resource_exists(org_id, resource_type, resource_id):
            raise RetentionResourceNotFound(resource_id)
        return await _create_hold_unlocked(
            org_id=org_id,
            region=region,
            resource_type=resource_type,
            resource_id=resource_id,
            reason=reason,
            actor_id=actor_id,
        )


async def _release_hold_unlocked(org_id: str, hold_id: str, actor_id: str) -> bool:
    holds = await reflect_table("retention_holds")
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                update(holds)
                .where(
                    holds.c.id == hold_id,
                    holds.c.organization_id == org_id,
                    holds.c.released_at.is_(None),
                )
                .values(released_at=func.now(), released_by=actor_id)
                .returning(holds.c.resource_type, holds.c.resource_id)
            )
        ).first()
    if row is None:
        return False
    await audit.log(
        "retention_hold",
        actor_id,
        "retention.hold.release",
        organization_id=org_id,
        resource_type=str(row[0]),
        resource_id=str(row[1]),
        payload={"hold_id": hold_id},
        decision="released",
    )
    return True


async def release_hold(org_id: str, hold_id: str, actor_id: str) -> bool:
    async with _org_retention_lock(org_id):
        return await _release_hold_unlocked(org_id, hold_id, actor_id)


async def _resource_is_held_unlocked(
    org_id: str, resource_type: str, resource_id: str
) -> bool:
    organization_hold, memory_holds, artifact_holds = await _active_hold_sets(org_id)
    if organization_hold:
        return True
    if resource_type == "memory":
        return str(resource_id) in memory_holds
    if resource_type == "artifact":
        return str(resource_id) in artifact_holds
    if resource_type == "workspace":
        rows = await list_holds(org_id, active_only=True)
        return any(
            row["resource_type"] == "workspace"
            and str(row["resource_id"]) == str(resource_id)
            for row in rows
        )
    return False


async def soft_delete_memory_if_allowed(memory_id: str, member: Member) -> bool:
    """Soft-delete one visible memory without racing a retention hold."""

    async with _org_retention_lock(member.organization_id):
        if await _resource_is_held_unlocked(
            member.organization_id, "memory", memory_id
        ):
            raise RetentionResourceHeld(memory_id)
        from core.memory_writes import soft_delete_memory_entry

        return await soft_delete_memory_entry(memory_id, member)


async def soft_delete_artifact_if_allowed(org_id: str, artifact_id: str) -> bool:
    """Soft-delete an artifact graph without racing a retention hold."""

    async with _org_retention_lock(org_id):
        organization_hold, _, artifact_holds = await _active_hold_sets(org_id)
        artifacts = await reflect_table("artifacts")
        async with engine.begin() as conn:
            derivative_ids = {
                str(row[0])
                for row in (
                    await conn.execute(
                        select(artifacts.c.id).where(
                            artifacts.c.organization_id == org_id,
                            cast(artifacts.c.parent_artifact_id, Text)
                            == str(artifact_id),
                        )
                    )
                ).all()
            }
        if organization_hold or ({str(artifact_id)} | derivative_ids) & artifact_holds:
            raise RetentionResourceHeld(artifact_id)
        from core.artifacts import soft_delete_artifact

        return await soft_delete_artifact(artifact_id, org_id)


async def soft_delete_all_memory(
    org_id: str, *, actor_id: str
) -> dict[str, int | bool]:
    """Hold-aware implementation of the admin bulk memory purge."""

    async with _org_retention_lock(org_id):
        organization_hold, memory_holds, _ = await _active_hold_sets(org_id)
        memory = await reflect_table("memory_entries")
        async with engine.begin() as conn:
            rows = (
                await conn.execute(
                    select(memory.c.id, memory.c.is_pinned).where(
                        memory.c.organization_id == org_id,
                        memory.c.is_deleted.is_(False),
                    )
                )
            ).mappings().all()
            pinned_ids = {
                str(row["id"]) for row in rows if bool(row.get("is_pinned"))
            }
            held_ids = {str(row["id"]) for row in rows} & memory_holds
            eligible_ids = [
                str(row["id"])
                for row in rows
                if str(row["id"]) not in pinned_ids
                and str(row["id"]) not in memory_holds
                and not organization_hold
            ]
            deleted_count = 0
            if eligible_ids:
                result = await conn.execute(
                    update(memory)
                    .where(
                        memory.c.organization_id == org_id,
                        memory.c.id.in_(eligible_ids),
                        memory.c.is_deleted.is_(False),
                    )
                    .values(is_deleted=True, updated_at=func.now())
                )
                deleted_count = int(result.rowcount or 0)
        evidence: dict[str, int | bool] = {
            "deleted": deleted_count,
            "pinned_excluded": len(pinned_ids),
            "held_excluded": len(rows) if organization_hold else len(held_ids),
            "organization_hold": organization_hold,
        }
        await audit.log(
            "settings_change",
            actor_id,
            "settings.memory.purge",
            organization_id=org_id,
            resource_type="memory",
            payload=evidence,
            decision="held" if organization_hold else "confirmed",
        )
        return evidence


async def _active_hold_sets(
    org_id: str,
) -> tuple[bool, set[str], set[str]]:
    rows = await list_holds(org_id, active_only=True)
    organization_hold = any(
        row["resource_type"] == "organization"
        and str(row["resource_id"]) == str(org_id)
        for row in rows
    )
    memory_holds = {
        str(row["resource_id"])
        for row in rows
        if row["resource_type"] == "memory"
    }
    artifact_holds = {
        str(row["resource_id"])
        for row in rows
        if row["resource_type"] == "artifact"
    }
    return organization_hold, memory_holds, artifact_holds


async def _memory_candidates(
    org_id: str,
    *,
    active_cutoff: datetime,
    deleted_cutoff: datetime,
) -> tuple[list[str], list[str], int]:
    memory = await reflect_table("memory_entries")
    columns = [memory.c.id]
    if "is_pinned" in memory.c:
        columns.append(memory.c.is_pinned)
    else:
        columns.append(literal(False).label("is_pinned"))
    async with engine.begin() as conn:
        active_rows = (
            await conn.execute(
                select(*columns).where(
                    memory.c.organization_id == org_id,
                    memory.c.is_deleted.is_(False),
                    memory.c.updated_at < active_cutoff,
                )
            )
        ).mappings().all()
        deleted_rows = (
            await conn.execute(
                select(*columns).where(
                    memory.c.organization_id == org_id,
                    memory.c.is_deleted.is_(True),
                    memory.c.updated_at < deleted_cutoff,
                )
            )
        ).mappings().all()

    pinned = {
        str(row["id"])
        for row in [*active_rows, *deleted_rows]
        if bool(row.get("is_pinned"))
    }
    active_ids = [str(row["id"]) for row in active_rows if str(row["id"]) not in pinned]
    deleted_ids = [
        str(row["id"]) for row in deleted_rows if str(row["id"]) not in pinned
    ]
    return active_ids, deleted_ids, len(pinned)


async def _artifact_candidates(
    org_id: str, cutoff: datetime
) -> list[dict[str, Any]]:
    artifacts = await reflect_table("artifacts")
    object_path_column = (
        artifacts.c.object_path
        if "object_path" in artifacts.c
        else artifacts.c["minio_path"]
    )
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(
                    artifacts.c.id,
                    artifacts.c.parent_artifact_id,
                    object_path_column.label("object_path"),
                ).where(
                    artifacts.c.organization_id == org_id,
                    artifacts.c.is_deleted.is_(True),
                    artifacts.c.updated_at < cutoff,
                )
            )
        ).mappings().all()
    return [dict(row) for row in rows]


async def _artifact_bundle(
    org_id: str, root_id: str
) -> tuple[list[str], set[str]]:
    """Return root/derived artifact ids and every distinct object path."""

    artifacts = await reflect_table("artifacts")
    versions = await reflect_table("artifact_versions")
    artifact_path = (
        artifacts.c.object_path
        if "object_path" in artifacts.c
        else artifacts.c["minio_path"]
    )
    version_path = (
        versions.c.object_path
        if "object_path" in versions.c
        else versions.c["minio_path"]
    )
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(artifacts.c.id, artifact_path.label("object_path")).where(
                    artifacts.c.organization_id == org_id,
                    or_(
                        cast(artifacts.c.id, Text) == str(root_id),
                        cast(artifacts.c.parent_artifact_id, Text) == str(root_id),
                    ),
                )
            )
        ).mappings().all()
        artifact_ids = [str(row["id"]) for row in rows]
        version_rows = []
        if artifact_ids:
            version_rows = (
                await conn.execute(
                    select(version_path.label("object_path")).where(
                        versions.c.organization_id == org_id,
                        cast(versions.c.artifact_id, Text).in_(artifact_ids),
                    )
                )
            ).mappings().all()
    paths = {
        str(row["object_path"])
        for row in [*rows, *version_rows]
        if row.get("object_path")
    }
    return artifact_ids, paths


def _local_artifact_path(object_path: str) -> Path:
    relative = object_path.removeprefix("local://").lstrip("/")
    candidate = (_LOCAL_ARTIFACT_ROOT / relative).resolve()
    if candidate != _LOCAL_ARTIFACT_ROOT and _LOCAL_ARTIFACT_ROOT not in candidate.parents:
        raise ValueError("local artifact path escapes the retention root")
    return candidate


async def _delete_artifact_object(org_id: str, object_path: str) -> None:
    if object_path.startswith("local://"):
        path = _local_artifact_path(object_path)
        path.unlink(missing_ok=True)
        # Remove empty version/artifact directories without crossing the fixed
        # scratch root. Missing/non-empty parents are harmless on retries.
        parent = path.parent
        while parent != _LOCAL_ARTIFACT_ROOT and _LOCAL_ARTIFACT_ROOT in parent.parents:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent
        return
    expected_prefix = f"artifacts/{org_id}/"
    if not object_path.startswith(expected_prefix):
        raise ValueError("artifact object key is outside its tenant prefix")
    await delete_object(object_path)


async def _purge_artifact_metadata(org_id: str, artifact_ids: list[str]) -> int:
    if not artifact_ids:
        return 0
    artifacts = await reflect_table("artifacts")
    versions = await reflect_table("artifact_versions")
    shares = await reflect_table("artifact_shares")
    comments = await reflect_table("comments")
    project_sources = await reflect_table("project_sources")
    source_chunks = await reflect_table("project_source_chunks")
    datasets = await reflect_table("datasets")
    research_runs = await reflect_table("research_runs")

    async with engine.begin() as conn:
        source_ids = [
            str(row[0])
            for row in (
                await conn.execute(
                    select(project_sources.c.id).where(
                        project_sources.c.organization_id == org_id,
                        cast(project_sources.c.artifact_id, Text).in_(artifact_ids),
                    )
                )
            ).all()
        ]
        if source_ids:
            await conn.execute(
                delete(source_chunks).where(
                    source_chunks.c.organization_id == org_id,
                    cast(source_chunks.c.source_id, Text).in_(source_ids),
                )
            )
            await conn.execute(
                delete(project_sources).where(
                    project_sources.c.organization_id == org_id,
                    cast(project_sources.c.id, Text).in_(source_ids),
                )
            )
        await conn.execute(
            delete(datasets).where(
                datasets.c.organization_id == org_id,
                cast(datasets.c.source_artifact_id, Text).in_(artifact_ids),
            )
        )
        await conn.execute(
            update(research_runs)
            .where(
                research_runs.c.organization_id == org_id,
                cast(research_runs.c.report_artifact_id, Text).in_(artifact_ids),
            )
            .values(report_artifact_id=None)
        )
        await conn.execute(
            delete(comments).where(
                comments.c.organization_id == org_id,
                comments.c.target_type == "artifact",
                comments.c.target_id.in_(artifact_ids),
            )
        )
        await conn.execute(
            delete(shares).where(
                shares.c.organization_id == org_id,
                cast(shares.c.artifact_id, Text).in_(artifact_ids),
            )
        )
        await conn.execute(
            delete(versions).where(
                versions.c.organization_id == org_id,
                cast(versions.c.artifact_id, Text).in_(artifact_ids),
            )
        )
        result = await conn.execute(
            delete(artifacts).where(
                artifacts.c.organization_id == org_id,
                cast(artifacts.c.id, Text).in_(artifact_ids),
            )
        )
    return int(result.rowcount or 0)


async def _run_retention_unlocked(
    org_id: str,
    *,
    dry_run: bool = True,
    actor_id: str = "retention_scheduler",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Evaluate or execute one organization's configured retention policy."""

    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    policy = await load_policy(org_id)
    result = RetentionResult(
        organization_id=org_id,
        dry_run=dry_run,
        retention_enabled=policy.enabled,
        configuration_valid=policy.configuration_valid,
    )
    result.memory_cutoff = (now - timedelta(days=policy.memory_days)).isoformat()
    result.deleted_memory_cutoff = (
        now - timedelta(days=policy.deleted_memory_days)
    ).isoformat()
    result.deleted_artifact_cutoff = (
        now - timedelta(days=policy.deleted_artifact_days)
    ).isoformat()

    organization_hold, memory_holds, artifact_holds = await _active_hold_sets(org_id)
    result.organization_hold = organization_hold
    if not policy.enabled or organization_hold:
        await audit.log(
            "retention_run",
            actor_id,
            "retention.run",
            organization_id=org_id,
            resource_type="organization",
            resource_id=org_id,
            payload=result.to_dict(),
            decision=(
                "invalid_configuration"
                if not policy.configuration_valid
                else "disabled"
                if not policy.enabled
                else "held"
            ),
        )
        return result.to_dict()

    active_ids, deleted_ids, pinned_count = await _memory_candidates(
        org_id,
        active_cutoff=now - timedelta(days=policy.memory_days),
        deleted_cutoff=now - timedelta(days=policy.deleted_memory_days),
    )
    result.pinned_memories_excluded = pinned_count
    held_memory_ids = (set(active_ids) | set(deleted_ids)) & memory_holds
    result.held_resources_excluded += len(held_memory_ids)
    active_ids = [item for item in active_ids if item not in memory_holds]
    deleted_ids = [item for item in deleted_ids if item not in memory_holds]
    result.memory_soft_delete_candidates = len(active_ids)
    result.memory_hard_delete_candidates = len(deleted_ids)

    artifact_rows = await _artifact_candidates(
        org_id, now - timedelta(days=policy.deleted_artifact_days)
    )
    artifact_root_ids: list[str] = []
    bundles: dict[str, tuple[list[str], set[str]]] = {}
    seen_artifacts: set[str] = set()
    candidate_ids = {str(row["id"]) for row in artifact_rows}
    root_rows = [
        row
        for row in artifact_rows
        if not row.get("parent_artifact_id")
        or str(row["parent_artifact_id"]) not in candidate_ids
    ]
    for row in root_rows:
        root_id = str(row["id"])
        if root_id in seen_artifacts:
            continue
        artifact_ids, paths = await _artifact_bundle(org_id, root_id)
        if not artifact_ids:
            continue
        seen_artifacts.update(artifact_ids)
        if set(artifact_ids) & artifact_holds:
            result.held_resources_excluded += 1
            continue
        artifact_root_ids.append(root_id)
        bundles[root_id] = (artifact_ids, paths)
    result.artifact_purge_candidates = len(artifact_root_ids)
    result.artifact_object_delete_candidates = sum(
        len(paths) for _, paths in bundles.values()
    )

    # Record the exact evaluated candidate counts before any irreversible I/O.
    await audit.log(
        "retention_run",
        actor_id,
        "retention.run.evaluate",
        organization_id=org_id,
        resource_type="organization",
        resource_id=org_id,
        payload=result.to_dict(),
        decision="dry_run" if dry_run else "execute",
    )
    if dry_run:
        return result.to_dict()

    memory = await reflect_table("memory_entries")
    memory_usage = await reflect_table("memory_usage_log")
    async with engine.begin() as conn:
        if active_ids:
            soft_result = await conn.execute(
                update(memory)
                .where(
                    memory.c.organization_id == org_id,
                    cast(memory.c.id, Text).in_(active_ids),
                    memory.c.is_deleted.is_(False),
                )
                .values(is_deleted=True, updated_at=now)
            )
            result.memory_soft_deleted = int(soft_result.rowcount or 0)
        if deleted_ids:
            await conn.execute(
                delete(memory_usage).where(
                    memory_usage.c.organization_id == org_id,
                    memory_usage.c.memory_id.in_(deleted_ids),
                )
            )
            hard_result = await conn.execute(
                delete(memory).where(
                    memory.c.organization_id == org_id,
                    cast(memory.c.id, Text).in_(deleted_ids),
                    memory.c.is_deleted.is_(True),
                )
            )
            result.memory_hard_deleted = int(hard_result.rowcount or 0)

    for root_id in artifact_root_ids:
        artifact_ids, object_paths = bundles[root_id]
        failed = False
        deleted_objects = 0
        for object_path in sorted(object_paths):
            try:
                await _delete_artifact_object(org_id, object_path)
                deleted_objects += 1
            except Exception:  # noqa: BLE001 - retain metadata and retry later
                failed = True
                log.exception(
                    "retention object deletion failed for org=%s artifact=%s",
                    org_id,
                    root_id,
                )
        result.artifact_objects_deleted += deleted_objects
        if failed:
            result.artifact_purge_failures += 1
            result.failed_artifact_ids.append(root_id)
            continue
        result.artifact_metadata_deleted += await _purge_artifact_metadata(
            org_id, artifact_ids
        )

    await audit.log(
        "retention_run",
        actor_id,
        "retention.run.complete",
        organization_id=org_id,
        resource_type="organization",
        resource_id=org_id,
        payload=result.to_dict(),
        decision=(
            "partial_failure" if result.artifact_purge_failures else "completed"
        ),
    )
    return result.to_dict()


async def run_retention(
    org_id: str,
    *,
    dry_run: bool = True,
    actor_id: str = "retention_scheduler",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Serialize and evaluate/execute one organization's retention policy."""

    async with _org_retention_lock(org_id):
        return await _run_retention_unlocked(
            org_id, dry_run=dry_run, actor_id=actor_id, now=now
        )


async def run_all_org_retention() -> list[dict[str, Any]]:
    """Run the daily policy for every real tenant without cross-tenant queries."""

    organizations = await reflect_table("organizations")
    async with engine.begin() as conn:
        org_ids = [
            str(row[0])
            for row in (await conn.execute(select(organizations.c.id))).all()
            if row[0]
        ]
    results: list[dict[str, Any]] = []
    for org_id in org_ids:
        try:
            results.append(
                await run_retention(
                    org_id, dry_run=False, actor_id="retention_scheduler"
                )
            )
        except Exception as exc:  # one tenant must not strand every later tenant
            log.exception("retention run failed for org=%s", org_id)
            try:
                await audit.log(
                    "retention_run",
                    "retention_scheduler",
                    "retention.run.failed",
                    organization_id=org_id,
                    resource_type="organization",
                    resource_id=org_id,
                    payload={"error_type": type(exc).__name__},
                    decision="failed",
                )
            except Exception:
                log.exception("retention failure audit also failed for org=%s", org_id)
    return results
