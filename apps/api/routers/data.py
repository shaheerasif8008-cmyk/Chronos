"""Data analysis workspace — HTTP router for datasets and analysis execution.

Endpoints:
    POST /datasets          — create a dataset from an uploaded source artifact
    GET  /datasets          — list tenant-scoped datasets
    GET  /datasets/{id}     — get a single dataset (cross-org → 404)
    POST /datasets/{id}/analyze — run analysis via tool_broker.execute("data.run", ...)

Every route calls permissions.check and uses the authenticated member's org_id
for all DB queries and artifact lookups.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import insert, select

from core import audit, permissions
from core.artifact_access import artifact_access
from core.auth import get_current_member
from core.config import settings
from core.db import engine, reflect_table
from core.models import AgentContext, Member
from core.memory_access import member_can_access_project
from core.project_access import member_can_edit_project

router = APIRouter(prefix="/datasets", tags=["data"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _require_dataset(member: Member, dataset_id: str) -> dict:
    """Load a dataset row tenant-scoped; raise 404 if not found or wrong org.

    Args:
        member: Authenticated member (provides org scope).
        dataset_id: UUID string of the dataset to fetch.

    Returns:
        Dataset row as a plain dict.

    Raises:
        HTTPException: 404 when the dataset does not exist or belongs to a different org.
    """
    datasets = await reflect_table("datasets")
    conditions = [
        datasets.c.id == dataset_id,
        datasets.c.organization_id == member.organization_id,
    ]
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                select(datasets).where(*conditions)
            )
        ).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Dataset not found")
    dataset = dict(row)
    project_id = dataset.get("project_id")
    if project_id:
        if not await member_can_access_project(member, str(project_id)):
            raise HTTPException(status_code=404, detail="Dataset not found")
    elif member.role not in {"admin", "owner"} and str(dataset.get("created_by")) != str(member.id):
        raise HTTPException(status_code=404, detail="Dataset not found")
    return dataset


def _infer_schema(content: bytes, mime_type: str, name: str) -> tuple[list[dict], int]:
    """Infer column schema and row count from CSV/XLSX/JSON bytes using pandas.

    Args:
        content: Raw file bytes.
        mime_type: MIME type string (used to choose the parser).
        name: Original filename (fallback for extension detection).

    Returns:
        Tuple of (columns_list, row_count) where each column dict has
        ``name`` and ``dtype`` keys. Returns ([], 0) when parsing fails.
    """
    try:
        import io
        import pandas as pd

        ext = (name or "").rsplit(".", 1)[-1].lower() if "." in (name or "") else ""
        lower_mime = (mime_type or "").lower()

        if "json" in lower_mime or ext == "json":
            df = pd.read_json(io.BytesIO(content))
        elif "xlsx" in lower_mime or "spreadsheet" in lower_mime or ext == "xlsx":
            df = pd.read_excel(io.BytesIO(content))
        else:
            # Default: CSV (handles text/csv, text/plain, application/csv, etc.)
            df = pd.read_csv(io.BytesIO(content))

        columns = [{"name": str(col), "dtype": str(df[col].dtype)} for col in df.columns]
        return columns, int(len(df))
    except Exception:
        return [], 0


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class CreateDatasetRequest(BaseModel):
    """Request body for POST /datasets.

    Attributes:
        source_artifact_id: UUID of the already-uploaded CSV/XLSX/JSON artifact.
        name: Optional human-readable dataset name (defaults to the artifact title).
    """

    source_artifact_id: str
    name: str | None = None
    project_id: str | None = None


class AnalyzeRequest(BaseModel):
    """Request body for POST /datasets/{id}/analyze.

    Attributes:
        code: Python analysis code to execute in the sandbox.
    """

    code: str


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/")
async def create_dataset(
    req: CreateDatasetRequest,
    member: Member = Depends(get_current_member),
) -> dict:
    """Create a dataset record from an existing source artifact.

    Parses the artifact to extract schema (column names + inferred dtypes) and
    row_count, then inserts a tenant-scoped ``datasets`` row.

    Args:
        req: CreateDatasetRequest with source_artifact_id and optional name.
        member: Authenticated member.

    Returns:
        Created dataset dict including inferred schema and row_count.

    Raises:
        HTTPException: 404 when the source artifact is not found in this org.
        HTTPException: 422 when the artifact content cannot be read.
    """
    await permissions.check(member, "create_dataset", settings.org_id)

    from core.artifacts import get_artifact, read_artifact_content

    artifact_meta = await get_artifact(req.source_artifact_id)
    if artifact_meta is None or artifact_meta.get("is_deleted"):
        raise HTTPException(status_code=404, detail="Source artifact not found")
    if str(artifact_meta.get("organization_id", "")) != str(member.organization_id):
        raise HTTPException(status_code=404, detail="Source artifact not found")
    visible, _ = await artifact_access(member, artifact_meta)
    if not visible:
        raise HTTPException(status_code=404, detail="Source artifact not found")

    content = await read_artifact_content(req.source_artifact_id)
    if content is None:
        raise HTTPException(status_code=422, detail="Could not read artifact content")

    mime_type = str(artifact_meta.get("mime_type") or "")
    artifact_title = str(artifact_meta.get("title") or "")
    dataset_name = req.name or artifact_title or "Untitled dataset"
    artifact_project_id = artifact_meta.get("project_id")
    project_id = str(req.project_id or artifact_project_id) if (req.project_id or artifact_project_id) else None
    if req.project_id and artifact_project_id and str(req.project_id) != str(artifact_project_id):
        raise HTTPException(status_code=422, detail="Dataset project must match its source artifact")
    if project_id and not await member_can_edit_project(member, project_id):
        raise HTTPException(status_code=404, detail="Project not found")

    columns, row_count = _infer_schema(content, mime_type, artifact_title)

    dataset_id = str(uuid.uuid4())
    datasets = await reflect_table("datasets")
    conditions = [datasets.c.organization_id == member.organization_id]
    if member.role not in {"admin", "owner"}:
        conditions.append(datasets.c.created_by == str(member.id))
    async with engine.begin() as conn:
        await conn.execute(
            insert(datasets).values(
                id=dataset_id,
                organization_id=member.organization_id,
                region=member.region,
                source_artifact_id=req.source_artifact_id,
                project_id=project_id,
                name=dataset_name,
                schema={"columns": columns},
                row_count=row_count,
                status="ready",
                created_by=str(member.id),
            )
        )

    await audit.log(
        "dataset_created",
        member.id,
        "create_dataset",
        organization_id=member.organization_id,
        payload={
            "dataset_id": dataset_id,
            "source_artifact_id": req.source_artifact_id,
            "project_id": project_id,
        },
    )

    return {
        "id": dataset_id,
        "organization_id": member.organization_id,
        "source_artifact_id": req.source_artifact_id,
        "project_id": project_id,
        "name": dataset_name,
        "schema": {"columns": columns},
        "row_count": row_count,
        "status": "ready",
    }


@router.get("/")
async def list_datasets(
    project_id: str | None = Query(default=None),
    member: Member = Depends(get_current_member),
) -> list[dict]:
    """List datasets for the authenticated member's org, newest first.

    Args:
        member: Authenticated member.

    Returns:
        List of dataset dicts.
    """
    await permissions.check(member, "list_datasets", settings.org_id)
    datasets = await reflect_table("datasets")
    conditions = [datasets.c.organization_id == member.organization_id]
    if project_id:
        if not await member_can_access_project(member, project_id):
            raise HTTPException(status_code=404, detail="Project not found")
        conditions.append(datasets.c.project_id == project_id)
    elif member.role not in {"admin", "owner"}:
        conditions.append(datasets.c.created_by == str(member.id))
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(datasets)
                .where(*conditions)
                .order_by(datasets.c.created_at.desc())
            )
        ).mappings().all()
    return [dict(r) for r in rows]


@router.get("/{dataset_id}")
async def get_dataset(
    dataset_id: str,
    member: Member = Depends(get_current_member),
) -> dict:
    """Get a single dataset by id.

    Cross-org access returns 404.

    Args:
        dataset_id: UUID of the dataset.
        member: Authenticated member.

    Returns:
        Dataset dict.
    """
    await permissions.check(member, "get_dataset", settings.org_id)
    return await _require_dataset(member, dataset_id)


@router.post("/{dataset_id}/analyze")
async def analyze_dataset(
    dataset_id: str,
    req: AnalyzeRequest,
    member: Member = Depends(get_current_member),
) -> dict:
    """Run analysis code against a dataset via tool_broker (audited, broker-routed).

    Builds an AgentContext from the authenticated member, then calls
    ``tool_broker.execute(agent, "data.run", {...})`` so the call is audited
    and all safety checks fire.

    Args:
        dataset_id: UUID of the dataset to analyze.
        req: AnalyzeRequest with the Python analysis code.
        member: Authenticated member.

    Returns:
        Dict with ``artifact_ids``, ``status``, and optional ``stdout_preview``.
    """
    await permissions.check(member, "analyze_dataset", settings.org_id)

    # Verify dataset exists in this org before routing to broker.
    dataset = await _require_dataset(member, dataset_id)

    agent = AgentContext(
        id=str(uuid.uuid4()),
        org_id=member.organization_id,
        member_id=str(member.id),
    )

    from core import tool_broker

    result = await tool_broker.execute(
        agent,
        "data.run",
        {"dataset_id": str(dataset["id"]), "code": req.code},
    )

    await audit.log(
        "dataset_analyzed",
        member.id,
        "analyze_dataset",
        organization_id=member.organization_id,
        payload={"dataset_id": dataset_id, "status": result.data.get("status")},
    )

    return {
        "dataset_id": dataset_id,
        "status": result.data.get("status"),
        "artifact_ids": result.data.get("artifact_ids", []),
        "stdout_preview": result.data.get("stdout_preview", ""),
        "summary": result.summary,
    }
