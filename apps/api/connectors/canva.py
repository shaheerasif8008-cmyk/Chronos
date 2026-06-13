from __future__ import annotations

"""Canva connector — governed access to the Canva Connect REST API.

Exposes high-level, broker-routed tools so the agent can create presentations
and designs, autofill brand templates with content, export to PPTX/PDF, and
read design metadata in the connected Canva account.

Live vs degraded is decided by whether the org has an active Canva connection
(a `connectors` row + vault credentials), not by a tier label — so the
connector degrades truthfully ("connect Canva first") instead of crashing when
no account is linked.

Honesty note: the Canva Connect API does not support free-form slide layout
from arbitrary instructions. Reliable programmatic content creation is via
`autofill` against a brand template's data fields; `create_design` produces a
presentation and returns an edit URL for a human to finish. Tool descriptions
say this so the agent does not overpromise.
"""

import asyncio
from typing import Any

from sqlalchemy import select

from core.config import settings
from core.db import engine, reflect_table
from core.models import ToolResult

_JOB_POLL_ATTEMPTS = 8
_JOB_POLL_DELAY_S = 1.5


class CanvaConnector:
    async def execute(self, tool: str, args: dict[str, Any]) -> ToolResult:
        tier = args.pop("__connector_tier", "live")
        org_id = str(args.pop("__org_id", settings.org_id) or settings.org_id)
        args.pop("__task_id", None)
        args.pop("__idempotency_key", None)
        action = tool.split(".", 1)[1] if "." in tool else tool

        # demo_mode forces local demo behaviour regardless of connection state.
        vault_ref = None if tier == "demo" else await self._connection_vault_ref(org_id)
        if not vault_ref:
            return ToolResult(
                data={"connected": False, "tool": tool},
                summary="Canva is not connected — link a Canva account under Connectors to create real designs.",
            )

        try:
            if action == "create_design":
                return await self._create_design(vault_ref, args)
            if action == "list_brand_templates":
                return await self._list_brand_templates(vault_ref, args)
            if action == "autofill":
                return await self._autofill(vault_ref, args)
            if action == "export":
                return await self._export(vault_ref, args)
            if action == "get_design":
                return await self._get_design(vault_ref, args)
        except RuntimeError as exc:
            return ToolResult(data={"error": str(exc)}, summary=f"{tool} failed: {exc}")
        raise ValueError(f"Unknown Canva tool: {tool}")

    async def _create_design(self, vault_ref: str, args: dict[str, Any]) -> ToolResult:
        title = str(args.get("title") or "Untitled presentation")
        preset = str(args.get("design_type") or "presentation")
        body: dict[str, Any] = {"design_type": {"type": "preset", "name": preset}, "title": title}
        if args.get("asset_id"):
            body["asset_id"] = str(args["asset_id"])
        data = await self._call(vault_ref, "POST", "/v1/designs", body=body)
        design = data.get("design") or {}
        urls = design.get("urls") or {}
        return ToolResult(
            data={
                "design_id": design.get("id"),
                "edit_url": urls.get("edit_url"),
                "view_url": urls.get("view_url"),
                "title": design.get("title") or title,
            },
            summary=f"Created Canva {preset} '{title}'",
        )

    async def _list_brand_templates(self, vault_ref: str, args: dict[str, Any]) -> ToolResult:
        params: dict[str, Any] = {}
        if args.get("query"):
            params["query"] = str(args["query"])
        data = await self._call(vault_ref, "GET", "/v1/brand-templates", params=params or None)
        items = data.get("items") or []
        templates = [
            {"id": t.get("id"), "title": t.get("title"), "view_url": (t.get("urls") or {}).get("view_url")}
            for t in items
        ]
        return ToolResult(
            data={"templates": templates, "count": len(templates)},
            summary=f"Found {len(templates)} Canva brand templates",
        )

    async def _autofill(self, vault_ref: str, args: dict[str, Any]) -> ToolResult:
        template_id = str(args.get("brand_template_id") or "")
        fill = args.get("data") or {}
        if not template_id:
            raise RuntimeError("canva.autofill requires brand_template_id")
        if not isinstance(fill, dict) or not fill:
            raise RuntimeError("canva.autofill requires a non-empty 'data' map of template field → value")
        body = {"brand_template_id": template_id, "data": fill}
        started = await self._call(vault_ref, "POST", "/v1/autofills", body=body)
        job = await self._poll_job(vault_ref, "/v1/autofills", started)
        result = job.get("result") or {}
        design = result.get("design") or {}
        return ToolResult(
            data={
                "status": job.get("status"),
                "design_id": design.get("id"),
                "edit_url": (design.get("url") or (design.get("urls") or {}).get("edit_url")),
            },
            summary=f"Autofill {job.get('status')} for template {template_id}",
        )

    async def _export(self, vault_ref: str, args: dict[str, Any]) -> ToolResult:
        design_id = str(args.get("design_id") or "")
        fmt = str(args.get("format") or "pptx").lower()
        if not design_id:
            raise RuntimeError("canva.export requires design_id")
        body = {"design_id": design_id, "format": {"type": fmt}}
        started = await self._call(vault_ref, "POST", "/v1/exports", body=body)
        job = await self._poll_job(vault_ref, "/v1/exports", started)
        urls = job.get("urls") or (job.get("result") or {}).get("urls") or []
        return ToolResult(
            data={"status": job.get("status"), "format": fmt, "download_urls": urls, "design_id": design_id},
            summary=f"Export {job.get('status')} ({fmt}) for design {design_id}",
        )

    async def _get_design(self, vault_ref: str, args: dict[str, Any]) -> ToolResult:
        design_id = str(args.get("design_id") or "")
        if not design_id:
            raise RuntimeError("canva.get_design requires design_id")
        data = await self._call(vault_ref, "GET", f"/v1/designs/{design_id}")
        design = data.get("design") or data
        return ToolResult(
            data={
                "design_id": design.get("id") or design_id,
                "title": design.get("title"),
                "urls": design.get("urls"),
            },
            summary=f"Fetched Canva design {design_id}",
        )

    async def _poll_job(self, vault_ref: str, base_endpoint: str, started: dict[str, Any]) -> dict[str, Any]:
        """Poll an async Canva job (autofill/export) until terminal or attempts exhausted."""
        job = started.get("job") or started
        job_id = job.get("id")
        status = job.get("status")
        if not job_id or status in {"success", "failed"}:
            return job
        for _ in range(_JOB_POLL_ATTEMPTS):
            await asyncio.sleep(_JOB_POLL_DELAY_S)
            polled = await self._call(vault_ref, "GET", f"{base_endpoint}/{job_id}")
            job = polled.get("job") or polled
            status = job.get("status")
            if status in {"success", "failed"}:
                return job
        return job

    async def _call(
        self,
        vault_ref: str,
        method: str,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # Reuse the generic OAuth2 HTTP engine for token refresh + base URL.
        from connectors.generic_http import call

        return await call(vault_ref, method, endpoint, params=params, body=body)

    async def _connection_vault_ref(self, org_id: str) -> str | None:
        try:
            connectors = await reflect_table("connectors")
            async with engine.begin() as conn:
                row = (
                    await conn.execute(
                        select(connectors.c.vault_ref).where(
                            connectors.c.organization_id == org_id,
                            connectors.c.provider == "canva",
                            connectors.c.status == "active",
                        ).limit(1)
                    )
                ).first()
            return str(row[0]) if row else None
        except Exception:
            return None


canva_connector = CanvaConnector()
