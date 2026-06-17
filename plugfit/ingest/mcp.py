import json
from .models import CanonicalTool, ToolManifest, ToolParam


def _parse_input_schema(
    schema: dict, warnings: list[str], tool_name: str
) -> dict[str, ToolParam]:
    """Convert MCP inputSchema (JSON Schema object) into {name: ToolParam}."""
    if not schema or schema.get("type") != "object":
        if schema:
            warnings.append(
                f"{tool_name}: inputSchema is not type=object — params may be incomplete"
            )
        return {}

    properties = schema.get("properties", {})
    required_set = set(schema.get("required", []))
    params: dict[str, ToolParam] = {}

    for pname, prop in properties.items():
        ptype = prop.get("type", "string")
        if isinstance(ptype, list):
            ptype = next((t for t in ptype if t != "null"), "string")

        params[pname] = ToolParam(
            type=ptype,
            description=prop.get("description", ""),
            required=pname in required_set,
            enum=prop.get("enum", []),
            default=prop.get("default"),
            items=prop.get("items", {}),
            properties=prop.get("properties", {}),
        )

    return params


# ── Tool object → CanonicalTool ───────────────────────────────────────────────


def _parse_mcp_tool(tool: dict, warnings: list[str]) -> CanonicalTool:
    name = tool.get("name", "unnamed_tool")
    description = (tool.get("description") or "").strip()

    if not description:
        warnings.append(
            f"{name}: empty description — agent cannot know when to call this"
        )
    elif len(description) < 20:
        warnings.append(
            f"{name}: description very short ({len(description)} chars) — likely not enough context"
        )

    input_schema = tool.get("inputSchema") or tool.get("input_schema") or {}
    params = _parse_input_schema(input_schema, warnings, name)

    return CanonicalTool(
        name=name,
        description=description,
        parameters=params,
        source="mcp",
        raw=tool,
    )


# ── Live MCP server fetch ─────────────────────────────────────────────────────


def _fetch_mcp_tools(url: str) -> tuple[list[dict], str]:
    """
    Send tools/list JSON-RPC to a live MCP server.
    Returns (tools_list, server_name).
    Raises on network or protocol error.
    """
    try:
        import httpx
    except ImportError:
        raise RuntimeError("httpx is required for live MCP fetching: pip install httpx")

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/list",
        "params": {},
    }

    # Try plain HTTP POST first (most common for local/dev MCP servers)
    try:
        resp = httpx.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, json.JSONDecodeError) as e:
        raise RuntimeError(f"MCP fetch failed for {url}: {e}")

    if "error" in data:
        raise RuntimeError(f"MCP server returned error: {data['error']}")

    result = data.get("result", {})

    # MCP spec allows tools at result.tools or result directly as a list
    if isinstance(result, list):
        tools = result
    else:
        tools = result.get("tools", [])

    server_info = result.get("serverInfo", {})  # type:ignore
    server_name = server_info.get("name", "")

    return tools, server_name


# ── Entry points ──────────────────────────────────────────────────────────────


def parse_mcp_tools_list(
    tools: list[dict], source_uri: str = "", title: str = ""
) -> ToolManifest:
    """Parse a bare list of MCP tool objects."""
    warnings: list[str] = []
    canonical = [_parse_mcp_tool(t, warnings) for t in tools]

    # Flag duplicate names — common in auto-generated MCPs
    seen: dict[str, int] = {}
    for t in canonical:
        seen[t.name] = seen.get(t.name, 0) + 1
    for name, count in seen.items():
        if count > 1:
            warnings.append(
                f"Duplicate tool name '{name}' appears {count} times — agent will be confused"
            )

    return ToolManifest(
        tools=canonical,
        source_type="mcp",
        source_uri=source_uri,
        title=title,
        warnings=warnings,
    )


def parse_mcp_response(response: dict, source_uri: str = "") -> ToolManifest:
    """Parse a full JSON-RPC tools/list response dict."""
    # Handle both raw result and wrapped response
    if "result" in response:
        result = response["result"]
    else:
        result = response

    if isinstance(result, list):
        tools = result
        title = ""
    else:
        tools = result.get("tools", [])
        server_info = result.get("serverInfo", {})
        title = server_info.get("name", "")

    return parse_mcp_tools_list(tools, source_uri=source_uri, title=title)


def fetch_and_parse_mcp(url: str) -> ToolManifest:
    """Fetch from a live MCP server URL and return a ToolManifest."""
    tools, server_name = _fetch_mcp_tools(url)
    manifest = parse_mcp_tools_list(tools, source_uri=url, title=server_name)
    return manifest
