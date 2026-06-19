"""
Server routes.

POST /servers/               — create a server (upload spec + metadata)
GET  /servers/               — list all servers for current tenant
GET  /servers/{id}           — get one server (status + scores)
GET  /servers/{id}/diff      — before/after tool manifest diff
GET  /servers/{id}/jobs      — job history for a server
POST /servers/{id}/reprocess — re-run the pipeline (after spec update)
DELETE /servers/{id}         — delete a server
"""

import json
import re
from typing import Tuple

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy import Result, select
from sqlalchemy.ext.asyncio import AsyncSession

from plugfit.ingest.ingest import ingest
from plugfit.app.routes.auth import get_current_user
from plugfit.app.models.models import (
    Job,
    JobStatus,
    Server,
    ServerStatus,
    SpecSource,
    User,
)
from plugfit.app.db.db import get_db
from plugfit.app.job.tasks import run_pipeline
from plugfit.app.schema.server import (
    JobOut,
    ManifestDiff,
    ScoreSummary,
    ServerOut,
    ToolDiff,
)

router = APIRouter(prefix="/servers", tags=["servers"])


def _slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:60] or "server"


async def _get_server_for_tenant(
    server_id: str, tenant: User, db: AsyncSession
) -> Server:
    """Fetch server by id, enforcing tenant ownership."""
    result = await db.execute(
        select(Server).where(Server.id == server_id, Server.user_id == tenant.id)
    )
    server = result.scalar_one_or_none()
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")
    return server


@router.post(
    "/",
    response_model=ServerOut,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a spec or provide a spec URL to create an MCP server",
)
async def create_server(
    spec_file: UploadFile | None = File(
        None,
        description="OpenAPI JSON/YAML or MCP tools-list JSON",
    ),
    spec_url: str | None = Form(
        None,
        description="URL to an OpenAPI/MCP specification",
    ),
    name: str = Form(...),
    base_url: str | None = Form(None),
    upstream_headers: str | None = Form(
        None,
        description="JSON object of headers",
    ),
    tenant: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ServerOut:
    if (spec_file is None) == (spec_url is None):
        raise HTTPException(
            status_code=400,
            detail="Provide exactly one of spec_file or spec_url",
        )

    from plugfit.app.config import settings

    if spec_file is not None:
        raw_bytes = await spec_file.read()

        if len(raw_bytes) > settings.MAX_SPEC_SIZE_BYTES:
            raise HTTPException(
                413,
                detail=f"Spec exceeds {settings.MAX_SPEC_SIZE_BYTES // 1024} KB limit",
            )

        raw_spec = raw_bytes.decode("utf-8", errors="replace")

        ingest_input = (
            json.loads(raw_spec) if raw_spec.strip().startswith("{") else raw_spec
        )

    else:
        ingest_input = spec_url
        raw_spec = spec_url
    headers: dict = {}

    if upstream_headers:
        try:
            headers = json.loads(upstream_headers)
        except json.JSONDecodeError:
            raise HTTPException(
                400,
                detail="upstream_headers must be valid JSON",
            )
    try:
        manifest = ingest(ingest_input)  # type:ignore
    except Exception as e:
        raise HTTPException(
            422,
            detail=f"Could not parse spec: {e}",
        )

    from plugfit.app.config import settings as cfg

    if len(manifest.tools) > cfg.MAX_TOOLS_PER_SPEC:
        raise HTTPException(
            422,
            detail=(
                f"Spec has {len(manifest.tools)} tools, max is {cfg.MAX_TOOLS_PER_SPEC}"
            ),
        )
    server = Server(
        user_id=tenant.id,
        name=name,
        slug=_slugify(name),
        status=ServerStatus.PROCESSING,
        spec_source=SpecSource(manifest.source_type),
        raw_spec=raw_spec,
        raw_manifest=manifest.to_dict(),
        tool_count_before=len(manifest.tools),
        base_url=base_url,
        upstream_headers=headers or None,
    )

    db.add(server)
    await db.flush()
    job = Job(
        server_id=server.id,
        status=JobStatus.PENDING,
        stage="pending",
    )

    db.add(job)
    await db.flush()

    await db.commit()
    await db.refresh(server)
    run_pipeline.delay(server.id, job.id)

    return _server_out(server)


@router.get(
    "/",
    response_model=list[ServerOut],
    summary="List all servers for the current User",
)
async def list_servers(
    tenant: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[ServerOut]:
    result: Result[Tuple[Server]] = await db.execute(
        select(Server)
        .where(Server.user_id == tenant.id)
        .order_by(Server.created_at.desc())
    )
    return [_server_out(s) for s in result.scalars().all()]


@router.get(
    "/{server_id}",
    response_model=ServerOut,
    summary="Get a single server",
)
async def get_server(
    server_id: str,
    tenant: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ServerOut:
    server: Server = await _get_server_for_tenant(server_id, tenant, db)
    return _server_out(server)


@router.get(
    "/{server_id}/score",
    response_model=ScoreSummary,
    summary="Get the score summary for a server",
)
async def get_score(
    server_id: str,
    tenant: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ScoreSummary:
    server = await _get_server_for_tenant(server_id, tenant, db)
    delta = None
    if server.score_before is not None and server.score_after is not None:
        delta = server.score_after - server.score_before
    return ScoreSummary(
        server_id=server.id,
        name=server.name,
        score_before=server.score_before,
        score_after=server.score_after,
        delta=delta,
        tool_count_before=server.tool_count_before,
        tool_count_after=server.tool_count_after,
        status=server.status,
    )


@router.get(
    "/{server_id}/diff",
    response_model=ManifestDiff,
    summary="Before/after tool manifest diff",
)
async def get_diff(
    server_id: str,
    tenant: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ManifestDiff:
    server: Server = await _get_server_for_tenant(server_id, tenant, db)
    if not server.raw_manifest or not server.cleaned_manifest:
        raise HTTPException(
            404, detail="Manifest not yet available — pipeline may still be running"
        )

    before_tools: dict[str, dict] = {
        t["name"]: t for t in server.raw_manifest.get("tools", [])
    }
    after_tools: dict[str, dict] = {
        t["name"]: t for t in server.cleaned_manifest.get("tools", [])
    }

    removed: list[str] = [n for n in before_tools if n not in after_tools]
    merged: list[str] = [
        n for n in before_tools if n not in after_tools and n not in removed
    ]

    diffs: list[ToolDiff] = []
    for name, after in after_tools.items():
        before = before_tools.get(name)
        desc_before = before["description"] if before else None
        desc_after = after.get("description", "")
        diffs.append(
            ToolDiff(
                name=name,
                description_before=desc_before,
                description_after=desc_after,
                param_count=len(after.get("parameters", {})),
                changed=desc_before != desc_after,
            )
        )

    return ManifestDiff(
        server_id=server.id,
        tools_before=len(before_tools),
        tools_after=len(after_tools),
        tools_removed=removed,
        tools_merged=merged,
        diffs=diffs,
    )


@router.get(
    "/{server_id}/jobs",
    response_model=list[JobOut],
    summary="Job history for a server",
)
async def get_jobs(
    server_id: str,
    tenant: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    server = await _get_server_for_tenant(server_id, tenant, db)
    result = await db.execute(
        select(Job).where(Job.server_id == server.id).order_by(Job.created_at.desc())
    )
    return [JobOut.model_validate(j) for j in result.scalars().all()]


@router.post(
    "/{server_id}/reprocess",
    response_model=JobOut,
    summary="Re-run the cleaning pipeline",
)
async def reprocess(
    server_id: str,
    tenant: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    server = await _get_server_for_tenant(server_id, tenant, db)
    if server.status == ServerStatus.PROCESSING:
        raise HTTPException(409, detail="Pipeline already running for this server")

    server.status = ServerStatus.PROCESSING
    job = Job(server_id=server.id, status=JobStatus.PENDING, stage="pending")
    db.add(job)
    await db.flush()
    await db.commit()
    await db.refresh(job)
    run_pipeline.delay(server.id, job.id)
    return JobOut.model_validate(job)


@router.delete(
    "/{server_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a server and all its data",
)
async def delete_server(
    server_id: str,
    tenant: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    server: Server = await _get_server_for_tenant(server_id, tenant, db)
    await db.delete(server)


def _server_out(server: Server) -> ServerOut:
    return ServerOut(
        id=server.id,
        tenant_id=server.user_id,
        name=server.name,
        slug=server.slug,
        status=server.status,
        spec_source=server.spec_source,
        score_before=server.score_before,
        score_after=server.score_after,
        tool_count_before=server.tool_count_before,
        tool_count_after=server.tool_count_after,
        base_url=server.base_url,
        mcp_path=server.mcp_path,
        created_at=server.created_at,
        updated_at=server.updated_at,
    )
