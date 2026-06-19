import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from plugfit.app.models.models import Server, ServerStatus
from plugfit.app.db.db import get_db
from plugfit.server.protocol import (
    INTERNAL_ERROR,
    METHOD_NOT_FOUND,
    TOOL_NOT_FOUND,
    INVALID_PARAMS,
    err,
    ok,
    make_initialize_result,
    make_tool_schema,
    make_tool_result,
)
from plugfit.server.proxy import ProxyError, call_tool
from plugfit.server.tool_handlers import get_tool_handler
from plugfit.app.config import settings

log = logging.getLogger("plugfit.mcp_http")

router = APIRouter(tags=["mcp"])


class HttpMcpSession:
    def __init__(self, server: Server):
        self.server = server
        # Use cleaned manifest if ready, fall back to raw
        manifest = server.cleaned_manifest or server.raw_manifest or {}
        self._tools: dict[str, dict] = {t["name"]: t for t in manifest.get("tools", [])}
        self._title = manifest.get("title", server.name)

    def dispatch(self, message: dict) -> dict | None:
        id_ = message.get("id")
        method = message.get("method", "")
        params = message.get("params") or {}

        # Notifications — no reply
        if id_ is None and method.startswith("notifications/"):
            return None

        try:
            if method == "initialize":
                return ok(id_, make_initialize_result(self._title))
            elif method == "tools/list":
                schemas = [make_tool_schema(t) for t in self._tools.values()]
                return ok(id_, {"tools": schemas})
            elif method == "tools/call":
                return self._tools_call(id_, params)
            elif method == "ping":
                return ok(id_, {})
            else:
                return err(id_, METHOD_NOT_FOUND, f"Method not found: {method}")
        except Exception as exc:
            log.exception("MCP dispatch error")
            return err(id_, INTERNAL_ERROR, str(exc))

    def _tools_call(self, id_: Any, params: dict) -> dict:
        name = params.get("name", "")
        args = params.get("arguments") or {}

        if name not in self._tools:
            return err(id_, TOOL_NOT_FOUND, f"Tool not found: '{name}'")

        tool = self._tools[name]
        missing = [
            k
            for k, p in tool.get("parameters", {}).items()
            if p.get("required") and k not in args
        ]
        if missing:
            return err(
                id_, INVALID_PARAMS, f"Missing required params for '{name}': {missing}"
            )
        try:
            base_url = self.server.base_url or ""
            headers = self.server.upstream_headers or {}
            if base_url or get_tool_handler(name):
                result_text = call_tool(tool, args, base_url, headers)
            else:
                result_text = json.dumps(
                    {
                        "note": "No base_url set for this server — configure it in settings or register a handler",
                        "tool": name,
                        "args": args,
                    }
                )

            return ok(id_, make_tool_result(result_text))

        except ProxyError as exc:
            return ok(id_, make_tool_result(str(exc), is_error=True))


async def _load_server(tenant_id: str, server_id: str, db: AsyncSession) -> Server:
    """Load server, verify it belongs to tenant_id, verify it's READY."""
    result = await db.execute(
        select(Server).where(
            Server.id == server_id,
            Server.user_id == tenant_id,
        )
    )
    server = result.scalar_one_or_none()
    if not server:
        raise HTTPException(404, detail="MCP server not found")
    if server.status not in (ServerStatus.READY, ServerStatus.PROCESSING):
        raise HTTPException(503, detail=f"Server not ready (status: {server.status})")
    return server


@router.post("/mcp/{tenant_id}/{server_id}")
async def mcp_post(
    tenant_id: str,
    server_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            err(None, -32700, "Invalid JSON"),
            status_code=400,
        )

    server = await _load_server(tenant_id, server_id, db)
    session = HttpMcpSession(server)

    if isinstance(body, list):
        responses = [session.dispatch(msg) for msg in body]
        responses = [r for r in responses if r is not None]
        return JSONResponse(responses)

    response = session.dispatch(body)
    if response is None:
        # Notification — 202 No Content
        return Response(status_code=202)

    return JSONResponse(response)


@router.get("/mcp/{tenant_id}/{server_id}/info")
async def mcp_info(
    tenant_id: str,
    server_id: str,
    db: AsyncSession = Depends(get_db),
):
    server = await _load_server(tenant_id, server_id, db)
    manifest = server.cleaned_manifest or server.raw_manifest or {}
    tools = manifest.get("tools", [])

    return {
        "server_id": server_id,
        "name": server.name,
        "status": server.status,
        "score_before": server.score_before,
        "score_after": server.score_after,
        "tool_count": len(tools),
        "tools": [
            {"name": t["name"], "description": t.get("description", "")} for t in tools
        ],
        "mcp_endpoint": f"/mcp/{tenant_id}/{server_id}",
        "claude_desktop_config": {
            "mcpServers": {
                server.slug: {
                    "command": "npx",
                    "args": [
                        "mcp-remote",
                        f"{settings.HOST}:{settings.PORT}/{settings.MCP_BASE_PATH}/{tenant_id}/{server_id}",
                    ],
                }
            }
        },
    }


@router.get("/mcp/{tenant_id}/{server_id}")
async def mcp_stream(
    tenant_id: str,
    server_id: str,
    db: AsyncSession = Depends(get_db),
):
    server = await _load_server(tenant_id, server_id, db)

    async def event_stream():
        import asyncio

        data = json.dumps(
            {
                "type": "connected",
                "server": server.name,
                "tools": server.tool_count_after or server.tool_count_before,
            }
        )
        yield f"data: {data}\n\n"
        while True:
            await asyncio.sleep(15)
            yield ": keepalive\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
