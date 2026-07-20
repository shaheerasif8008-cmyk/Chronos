"""Durable, bounded ZIP exports for explicitly shared project artifacts."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
from io import BytesIO
import json
import re
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from sqlalchemy import select

from core.artifacts import read_artifact_content
from core.config import settings
from core.db import engine, reflect_table


class ProjectExportError(ValueError):
    """A project cannot be exported within the configured safety limits."""


def _safe_name(value: str, fallback: str) -> str:
    name = value.replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r"[^A-Za-z0-9._ ()-]+", "_", name).strip(" .")[:180]
    return name or fallback


def _dedupe_name(name: str, used: set[str]) -> str:
    candidate = name
    stem, dot, suffix = name.rpartition(".")
    if not dot:
        stem, suffix = name, ""
    counter = 2
    while candidate.lower() in used:
        candidate = f"{stem} ({counter}){'.' + suffix if suffix else ''}"
        counter += 1
    used.add(candidate.lower())
    return candidate


def _zip_bytes(project: dict[str, Any], items: list[tuple[dict[str, Any], bytes]]) -> bytes:
    used: set[str] = set()
    manifest_items: list[dict[str, Any]] = []
    output = BytesIO()
    with ZipFile(output, mode="w", compression=ZIP_DEFLATED, compresslevel=6) as archive:
        for index, (meta, content) in enumerate(items, start=1):
            fallback = f"artifact-{index}-{str(meta['id'])[:8]}"
            path = f"artifacts/{_dedupe_name(_safe_name(str(meta.get('title') or ''), fallback), used)}"
            info = ZipInfo(path)
            info.compress_type = ZIP_DEFLATED
            # Stable timestamps avoid leaking host-local clock details and keep
            # identical content deterministic enough for audit comparisons.
            info.date_time = (1980, 1, 1, 0, 0, 0)
            archive.writestr(info, content)
            manifest_items.append({
                "id": str(meta["id"]),
                "path": path,
                "title": meta.get("title"),
                "kind": meta.get("kind"),
                "mime_type": meta.get("mime_type"),
                "version": int(meta.get("version") or 1),
                "size_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
                "created_at": str(meta.get("created_at") or ""),
                "updated_at": str(meta.get("updated_at") or ""),
            })
        manifest = {
            "schema_version": 1,
            "project": {"id": str(project["id"]), "name": project.get("name")},
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "scope": "artifacts explicitly shared to this project",
            "artifact_count": len(manifest_items),
            "artifacts": manifest_items,
        }
        archive.writestr(
            "manifest.json",
            json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True).encode("utf-8"),
        )
    return output.getvalue()


async def build_project_bundle(project_id: str, org_id: str) -> tuple[bytes, dict[str, Any]]:
    """Build a ZIP of only artifacts explicitly assigned to a project.

    Conversation- or task-linked private artifacts are deliberately excluded:
    exporting them into a project-visible bundle would widen their audience.
    """
    projects = await reflect_table("projects")
    artifacts = await reflect_table("artifacts")
    async with engine.begin() as conn:
        project_row = (
            await conn.execute(
                select(projects).where(
                    projects.c.id == project_id,
                    projects.c.organization_id == org_id,
                )
            )
        ).mappings().first()
        rows = (
            await conn.execute(
                select(artifacts)
                .where(
                    artifacts.c.organization_id == org_id,
                    artifacts.c.project_id == project_id,
                    artifacts.c.is_deleted == False,  # noqa: E712
                    artifacts.c.kind != "project_bundle",
                )
                .order_by(artifacts.c.created_at.asc())
                .limit(settings.project_export_max_artifacts + 1)
            )
        ).mappings().all()
    if project_row is None:
        raise ProjectExportError("Project not found")
    if len(rows) > settings.project_export_max_artifacts:
        raise ProjectExportError(
            f"Project has more than {settings.project_export_max_artifacts} explicitly shared artifacts."
        )

    total = 0
    items: list[tuple[dict[str, Any], bytes]] = []
    for row in rows:
        meta = dict(row)
        declared = int(meta.get("size_bytes") or 0)
        if declared > settings.project_export_max_bytes - total:
            raise ProjectExportError("Project artifacts exceed the configured export size limit.")
        content = await read_artifact_content(str(meta["id"]))
        if content is None:
            raise ProjectExportError(f"Artifact {meta['id']} is unavailable in object storage.")
        total += len(content)
        if total > settings.project_export_max_bytes:
            raise ProjectExportError("Project artifacts exceed the configured export size limit.")
        items.append((meta, content))

    bundle = await asyncio.to_thread(_zip_bytes, dict(project_row), items)
    if len(bundle) > settings.project_export_max_bytes:
        raise ProjectExportError("Compressed project bundle exceeds the configured export size limit.")
    summary = {
        "project_name": project_row.get("name"),
        "artifact_count": len(items),
        "source_bytes": total,
        "bundle_bytes": len(bundle),
        "scope": "explicit_project_artifacts",
    }
    return bundle, summary
