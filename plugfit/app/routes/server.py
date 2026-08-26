"""
Server routes.

POST /servers/               — create a server (upload spec + metadata)
GET  /servers/               — list all servers for current tenant
GET  /servers/{id}           — get one server (status + scores)
GET  /servers/{id}/diff      — before/after tool manifest diff
GET  /servers/{id}/jobs      — job history for a server
POST /servers/{id}/reprocess — re-run the pipeline (after spec update)
POST /servers/{id}/eval      — re-run just the MCP eval (rate-limited)
POST /servers/{id}/heal      - Run the self-healing loop on the cleaned manifest
DELETE /servers/{id}         — delete a server
"""

import asyncio
import ipaddress
import json
import logging
import re
import socket
from typing import Tuple
from urllib.parse import urlparse

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
from plugfit.app.job.tasks import run_eval_only, run_heal_only, run_pipeline
from plugfit.app.schema.server import (
    JobOut,
    ManifestDiff,
    ScoreSummary,
    ServerOut,
    ToolDiff,
)
from plugfit.app.utils.rate_limit import rate_limit

EVAL_RATE_LIMIT = 3
EVAL_RATE_WINDOW = 60 * 60

HEAL_RATE_LIMIT = 1
HEAL_RATE_WINDOW = 60 * 60

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/servers", tags=["servers"])


def _slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:60] or "server"


async def _unique_server_slug(name: str, tenant_id: str, db: AsyncSession) -> str:
    import secrets

    slug = _slugify(name)
    result = await db.execute(
        select(Server).where(Server.user_id == tenant_id, Server.slug == slug)
    )
    if result.scalar_one_or_none():
        slug = f"{slug[:51]}-{secrets.token_hex(4)}"
    return slug


async def _validate_spec_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(400, detail="spec_url must be http:// or https://")

    hostname = parsed.hostname
    if not hostname:
        raise HTTPException(400, detail="spec_url must include a hostname")

    try:
        loop = asyncio.get_event_loop()
        infos = await loop.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise HTTPException(400, detail=f"Could not resolve spec_url host: {exc}")

    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise HTTPException(
                400,
                detail=(
                    f"spec_url resolves to a non-public address ({ip}) — not allowed"
                ),
            )


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
    project_id: str | None = Form(
        None,
        description="Optional project to assign this server to on creation",
    ),
    tenant: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ServerOut:
    logger.info(f"Server creation request: name='{name}', user_id={tenant.id}")
    if project_id is not None:
        from plugfit.app.routes.project import _get_project_for_tenant

        await _get_project_for_tenant(project_id, tenant, db)

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

        try:
            ingest_input = json.loads(raw_spec)
        except json.JSONDecodeError:
            import yaml

            try:
                ingest_input = yaml.safe_load(raw_spec)
            except yaml.YAMLError:
                logger.warning("Server creation failed - spec is neither JSON nor YAML")
                raise HTTPException(
                    422, detail="Spec file is neither valid JSON nor valid YAML"
                )
        if not isinstance(ingest_input, (dict, list)):
            logger.warning(
                f"Server creation failed - spec parsed to {type(ingest_input).__name__}, not an object/array"
            )
            raise HTTPException(
                422, detail="Spec file must contain a JSON/YAML object or array"
            )

    else:
        logger.debug(f"Processing spec from URL: {spec_url}")
        await _validate_spec_url(spec_url)  # type: ignore[arg-type]
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
        manifest = await asyncio.to_thread(ingest, ingest_input)  # type:ignore
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
        slug=await _unique_server_slug(name, tenant.id, db),
        project_id=project_id,
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
        score_method=server.score_method,
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
    return _compute_manifest_diff(
        server.id, server.raw_manifest, server.cleaned_manifest
    )


def _tool_key(t: dict) -> str:
    """Stable identity for a tool: tool_id when present, else name (legacy
    manifests ingested before ids existed)."""
    return t.get("tool_id") or t["name"]


def _compute_manifest_diff(
    server_id: str, raw_manifest: dict, cleaned_manifest: dict
) -> ManifestDiff:
    """Diff before/after manifests aligned on stable tool ids, so a renamed
    tool matches its pre-rename self instead of showing up as merged/new."""
    before_tools: dict[str, dict] = {
        _tool_key(t): t for t in raw_manifest.get("tools", [])
    }
    after_tools: dict[str, dict] = {
        _tool_key(t): t for t in cleaned_manifest.get("tools", [])
    }

    meta = cleaned_manifest.get("_cleaning_meta", {})
    removed = meta.get("dropped", [])
    removed_keys = set(meta.get("dropped_ids") or removed)
    merged_keys = set(meta.get("merged_ids") or meta.get("merged", []))

    # A tool counts as merged only if the cleaner actually merged it away —
    # or, for legacy manifests without ids, if it vanished without being
    # dropped (the old name-based heuristic).
    merged: list[str] = [
        before_tools[k]["name"]
        for k in before_tools
        if k not in after_tools
        and k not in removed_keys
        and (not merged_keys or k in merged_keys)
    ]

    diffs: list[ToolDiff] = []
    for key, after in after_tools.items():
        before = before_tools.get(key)
        desc_before = before["description"] if before else None
        desc_after = after.get("description", "")
        diffs.append(
            ToolDiff(
                name=after["name"],
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
        server_id=server_id,
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


@router.post(
    "/{server_id}/eval",
    response_model=JobOut,
    summary="Re-run just the MCP eval (before/after tool-call accuracy)",
    dependencies=[
        Depends(
            rate_limit(
                lambda request,
                tenant: f"eval:{tenant.id}:{request.path_params['server_id']}",
                limit=EVAL_RATE_LIMIT,
                window_seconds=EVAL_RATE_WINDOW,
            )
        )
    ],
)
async def trigger_eval(
    server_id: str,
    tenant: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    logger.info(f"Eval request for server: {server_id}, user_id: {tenant.id}")
    server = await _get_server_for_tenant(server_id, tenant, db)

    if server.status == ServerStatus.PROCESSING:
        logger.warning(f"Eval failed - pipeline already running: {server_id}")
        raise HTTPException(409, detail="Pipeline already running for this server")

    if not server.raw_manifest or not server.cleaned_manifest:
        raise HTTPException(
            409,
            detail="Server has no cleaned manifest yet — run the full pipeline first",
        )

    server.status = ServerStatus.PROCESSING
    job = Job(server_id=server.id, status=JobStatus.PENDING, stage="pending")
    db.add(job)
    await db.flush()
    await db.commit()
    await db.refresh(job)
    run_eval_only.delay(server.id, job.id)  # type:ignore
    logger.info(f"Eval initiated - server: {server_id}, job_id: {job.id}")
    return JobOut.model_validate(job)


@router.post(
    "/{server_id}/heal",
    response_model=JobOut,
    summary="Run the self-healing loop on the cleaned manifest",
    dependencies=[
        Depends(
            rate_limit(
                lambda request,
                tenant: f"heal:{tenant.id}:{request.path_params['server_id']}",
                limit=HEAL_RATE_LIMIT,
                window_seconds=HEAL_RATE_WINDOW,
            )
        )
    ],
)
async def trigger_heal(
    server_id: str,
    tenant: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    logger.info(f"Heal request for server: {server_id}, user_id: {tenant.id}")
    server = await _get_server_for_tenant(server_id, tenant, db)

    if server.status == ServerStatus.PROCESSING:
        logger.warning(f"Heal failed - pipeline already running: {server_id}")
        raise HTTPException(409, detail="Pipeline already running for this server")

    if not server.cleaned_manifest:
        raise HTTPException(
            409,
            detail="Server has no cleaned manifest yet — run the full pipeline first",
        )

    server.status = ServerStatus.PROCESSING
    job = Job(server_id=server.id, status=JobStatus.PENDING, stage="pending")
    db.add(job)
    await db.flush()
    await db.commit()
    await db.refresh(job)
    run_heal_only.delay(server.id, job.id)  # type:ignore
    logger.info(f"Heal initiated - server: {server_id}, job_id: {job.id}")
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
        project_id=server.project_id,
        name=server.name,
        slug=server.slug,
        status=server.status,
        spec_source=server.spec_source,
        score_before=server.score_before,
        score_after=server.score_after,
        score_method=server.score_method,
        score_healed=server.score_healed,
        tool_count_before=server.tool_count_before,
        tool_count_after=server.tool_count_after,
        base_url=server.base_url,
        mcp_path=server.mcp_path,
        created_at=server.created_at,
        updated_at=server.updated_at,
    )
