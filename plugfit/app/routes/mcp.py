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

        log.info(
            f"MCP dispatch request: server={self.server.id} method={method} id={id_} "
            f"tool_count={len(self._tools)}"
        )
        try:
            if method == "initialize":
                log.debug("MCP initialize requested")
                return ok(id_, make_initialize_result(self._title))
            elif method == "tools/list":
                log.debug("MCP tools list requested")
                schemas = [make_tool_schema(t) for t in self._tools.values()]
                return ok(id_, {"tools": schemas})
            elif method == "tools/call":
                tool_name = params.get("name")
                log.info(
                    f"MCP tool call request: server={self.server.id} tool={tool_name}"
                )
                log.info(
                    f"MCP tool selected: server={self.server.id} selected_tool={tool_name} tool_defined={tool_name in self._tools}"
                )
                return self._tools_call(id_, params)
            elif method == "ping":
                log.debug("MCP ping requested")
                return ok(id_, {})
            else:
                log.warning(f"MCP method not found: {method}")
                return err(id_, METHOD_NOT_FOUND, f"Method not found: {method}")
        except Exception as exc:
            log.exception("MCP dispatch error")
            return err(id_, INTERNAL_ERROR, str(exc))

    def _tools_call(self, id_: Any, params: dict) -> dict:
        name = params.get("name", "")
        args = params.get("arguments") or {}

        if name not in self._tools:
            log.warning(
                f"MCP tool not found: server={self.server.id} tool={name}"
            )
            return err(id_, TOOL_NOT_FOUND, f"Tool not found: '{name}'")

        tool = self._tools[name]
        allowed_params = set(tool.get("parameters", {}).keys())
        invalid_args = [k for k in args if k not in allowed_params]
        if invalid_args:
            log.warning(
                f"MCP tool call invalid params: server={self.server.id} tool={name} invalid={invalid_args}"
            )
            return err(
                id_, INVALID_PARAMS, f"Invalid params for '{name}': {invalid_args}"
            )

        missing = [
            k
            for k, p in tool.get("parameters", {}).items()
            if p.get("required") and k not in args
        ]
        if missing:
            log.warning(
                f"MCP tool call missing required params: server={self.server.id} tool={name} missing={missing}"
            )
            return err(
                id_, INVALID_PARAMS, f"Missing required params for '{name}': {missing}"
            )
        try:
            base_url = self.server.base_url or ""
            headers = self.server.upstream_headers or {}
            if base_url or get_tool_handler(name):
                log.debug(
                    f"Calling tool: server={self.server.id} tool={name} base_url_set={bool(base_url)}"
                )
                result_text = call_tool(tool, args, base_url, headers)
            else:
                log.warning(
                    f"No base_url or registered handler for MCP tool: server={self.server.id} tool={name}"
                )
                result_text = json.dumps(
                    {
                        "note": "No base_url set for this server — configure it in settings or register a handler",
                        "tool": name,
                        "args": args,
                    }
                )

            log.info(
                f"MCP tool call succeeded: server={self.server.id} tool={name}"
            )
            return ok(id_, make_tool_result(result_text))

        except ProxyError as exc:
            log.error(
                f"MCP proxy error: server={self.server.id} tool={name} error={str(exc)}"
            )
            return ok(id_, make_tool_result(str(exc), is_error=True))


async def _load_server(tenant_id: str, server_id: str, db: AsyncSession) -> Server:
    """Load server, verify it belongs to tenant_id, verify it's READY."""
    log.debug(f"Loading MCP server: {server_id}, tenant_id: {tenant_id}")
    result = await db.execute(
        select(Server).where(
            Server.id == server_id,
            Server.user_id == tenant_id,
        )
    )
    server = result.scalar_one_or_none()
    if not server:
        log.warning(f"MCP server not found: {server_id}, tenant_id: {tenant_id}")
        raise HTTPException(404, detail="MCP server not found")
    if server.status not in (ServerStatus.READY, ServerStatus.PROCESSING):
        log.warning(f"MCP server not ready: {server_id}, status: {server.status}")
        raise HTTPException(503, detail=f"Server not ready (status: {server.status})")
    return server


@router.post("/mcp/{tenant_id}/{server_id}")
async def mcp_post(
    tenant_id: str,
    server_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    log.debug(f"MCP POST request: tenant_id={tenant_id}, server_id={server_id}")
    try:
        body = await request.json()
    except Exception as e:
        log.warning(f"MCP POST failed - invalid JSON: {str(e)}")
        return JSONResponse(
            err(None, -32700, "Invalid JSON"),
            status_code=400,
        )

    server = await _load_server(tenant_id, server_id, db)
    session = HttpMcpSession(server)

    if isinstance(body, list):
        log.debug(f"MCP batch request with {len(body)} messages")
        responses = [session.dispatch(msg) for msg in body]
        responses = [r for r in responses if r is not None]
        return JSONResponse(responses)

    response = session.dispatch(body)
    if response is None:
        # Notification — 202 No Content
        log.info(
            f"MCP notification processed: tenant_id={tenant_id} server_id={server_id}"
        )
        return Response(status_code=202)

    log.debug(
        f"MCP response ready: tenant_id={tenant_id} server_id={server_id} response_id={body.get('id')}"
    )
    return JSONResponse(response)


@router.get("/mcp/{tenant_id}/{server_id}/info")
async def mcp_info(
    tenant_id: str,
    server_id: str,
    db: AsyncSession = Depends(get_db),
):
    log.debug(f"MCP info request: tenant_id={tenant_id}, server_id={server_id}")
    server = await _load_server(tenant_id, server_id, db)
    manifest = server.cleaned_manifest or server.raw_manifest or {}
    tools = manifest.get("tools", [])
    log.debug(f"MCP info retrieved - {len(tools)} tools, status: {server.status}")
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
    log.info(
        f"MCP stream connection initiated: tenant_id={tenant_id}, server_id={server_id}"
    )
    server = await _load_server(tenant_id, server_id, db)

    async def event_stream():
        import asyncio

        try:
            data = json.dumps(
                {
                    "type": "connected",
                    "server": server.name,
                    "tools": server.tool_count_after or server.tool_count_before,
                }
            )
            yield f"data: {data}\n\n"
            log.debug(f"MCP stream connected - server: {server.name}")
            while True:
                await asyncio.sleep(15)
                yield ": keepalive\n\n"
        except Exception as e:
            log.error(f"MCP stream error: {str(e)}")
            raise

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
