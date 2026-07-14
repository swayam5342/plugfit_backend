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
import logging
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

logger = logging.getLogger(__name__)

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
    logger.error(spec_url, name, base_url, upstream_headers, tenant)
    logger.info(f"Server creation request: name='{name}', user_id={tenant.id}")
    if (spec_file is None) == (spec_url is None):
        logger.warning(
            f"Server creation failed - ambiguous spec source (file={spec_file is not None}, url={spec_url is not None})"
        )
        raise HTTPException(
            status_code=400,
            detail="Provide exactly one of spec_file or spec_url",
        )

    from plugfit.app.config import settings

    if spec_file is not None:
        logger.debug(f"Processing uploaded spec file: {spec_file.filename}")
        raw_bytes = await spec_file.read()

        if len(raw_bytes) > settings.MAX_SPEC_SIZE_BYTES:
            logger.warning(
                f"Server creation failed - spec file too large: {len(raw_bytes)} bytes (max: {settings.MAX_SPEC_SIZE_BYTES})"
            )
            raise HTTPException(
                413,
                detail=f"Spec exceeds {settings.MAX_SPEC_SIZE_BYTES // 1024} KB limit",
            )

        raw_spec = raw_bytes.decode("utf-8", errors="replace")

        ingest_input = (
            json.loads(raw_spec) if raw_spec.strip().startswith("{") else raw_spec
        )

    else:
        logger.debug(f"Processing spec from URL: {spec_url}")
        ingest_input = spec_url
        raw_spec = spec_url
    headers: dict = {}

    if upstream_headers:
        try:
            headers = json.loads(upstream_headers)
        except json.JSONDecodeError:
            logger.warning(f"Server creation failed - invalid upstream_headers JSON")
            raise HTTPException(
                400,
                detail="upstream_headers must be valid JSON",
            )
    try:
        manifest = ingest(ingest_input)  # type:ignore
    except Exception as e:
        logger.warning(f"Server creation failed - spec parsing error: {str(e)}")
        raise HTTPException(
            422,
            detail=f"Could not parse spec: {e}",
        )

    from plugfit.app.config import settings as cfg

    if len(manifest.tools) > cfg.MAX_TOOLS_PER_SPEC:
        logger.warning(
            f"Server creation failed - too many tools: {len(manifest.tools)} (max: {cfg.MAX_TOOLS_PER_SPEC})"
        )
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
    run_pipeline.delay(server.id, job.id)  # type:ignore
    logger.info(
        f"Server created successfully: {name} (ID: {server.id}, tools: {len(manifest.tools)}, user_id: {tenant.id})"
    )
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
    logger.debug(f"Listing servers for user_id: {tenant.id}")
    result: Result[Tuple[Server]] = await db.execute(
        select(Server)
        .where(Server.user_id == tenant.id)
        .order_by(Server.created_at.desc())
    )
    servers = [_server_out(s) for s in result.scalars().all()]
    logger.debug(f"Found {len(servers)} servers for user_id: {tenant.id}")
    return servers


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
    logger.debug(f"Fetching server: {server_id}, user_id: {tenant.id}")
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
    logger.debug(
        f"Fetching score summary for server: {server_id}, user_id: {tenant.id}"
    )
    server = await _get_server_for_tenant(server_id, tenant, db)
    delta = None
    if server.score_before is not None and server.score_after is not None:
        delta = server.score_after - server.score_before
    logger.debug(
        f"Score summary retrieved - before: {server.score_before}, after: {server.score_after}, delta: {delta}"
    )
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
    logger.debug(
        f"Fetching manifest diff for server: {server_id}, user_id: {tenant.id}"
    )
    server: Server = await _get_server_for_tenant(server_id, tenant, db)
    if not server.raw_manifest or not server.cleaned_manifest:
        logger.warning(
            f"Manifest diff requested but manifests not yet available: {server_id}"
        )
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
    logger.debug(
        f"Manifest diff computed - tools: before={len(before_tools)}, after={len(after_tools)}, removed={len(removed)}"
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
    logger.debug(f"Fetching job history for server: {server_id}, user_id: {tenant.id}")
    server = await _get_server_for_tenant(server_id, tenant, db)
    result = await db.execute(
        select(Job).where(Job.server_id == server.id).order_by(Job.created_at.desc())
    )
    jobs = [JobOut.model_validate(j) for j in result.scalars().all()]
    logger.debug(f"Job history retrieved - count: {len(jobs)}")
    return jobs


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
    logger.info(f"Reprocess request for server: {server_id}, user_id: {tenant.id}")
    server = await _get_server_for_tenant(server_id, tenant, db)
    if server.status == ServerStatus.PROCESSING:
        logger.warning(f"Reprocess failed - pipeline already running: {server_id}")
        raise HTTPException(409, detail="Pipeline already running for this server")

    server.status = ServerStatus.PROCESSING
    job = Job(server_id=server.id, status=JobStatus.PENDING, stage="pending")
    db.add(job)
    await db.flush()
    await db.commit()
    await db.refresh(job)
    run_pipeline.delay(server.id, job.id)  # type:ignore
    logger.info(f"Reprocess initiated - server: {server_id}, job_id: {job.id}")
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
    logger.info(f"Delete server request: {server_id}, user_id: {tenant.id}")
    server: Server = await _get_server_for_tenant(server_id, tenant, db)
    await db.delete(server)
    logger.info(f"Server deleted successfully: {server_id}")


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
