"""
Lightweight MCP client for the eval harness.

Talks to an MCP server in one of two modes:
  1. HTTP  — POST JSON-RPC to a URL (platform servers)
  2. Inline — calls the HttpMcpSession directly without HTTP (fast, for testing)

The eval harness uses this to:
  - Fetch the tools/list (so the agent knows what tools exist)
  - Execute tool calls the agent makes and return real results
"""

import json
import logging
from typing import Any

log = logging.getLogger("plugfit.eval.mcp_client")


class MCPError(Exception):
    pass


class HttpMCPClient:
    """
    Connects to a PlugFit platform MCP HTTP endpoint.
    URL format: http://host/mcp/{tenant_id}/{server_id}
    """

    def __init__(self, url: str, timeout: int = 15):
        self.url     = url.rstrip("/")
        self.timeout = timeout
        self._id     = 0

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    def _call(self, method: str, params: dict) -> Any:
        import urllib.request, urllib.error
        payload = json.dumps({
            "jsonrpc": "2.0",
            "id":      self._next_id(),
            "method":  method,
            "params":  params,
        }).encode()

        req = urllib.request.Request(
            self.url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            raise MCPError(f"HTTP {e.code}: {e.read().decode()[:200]}")
        except Exception as e:
            raise MCPError(f"Network error: {e}")

        if "error" in data:
            raise MCPError(f"MCP error {data['error']['code']}: {data['error']['message']}")

        return data.get("result")

    def initialize(self) -> dict:
        return self._call("initialize", {
            "protocolVersion": "2024-11-05",
            "clientInfo": {"name": "plugfit-eval", "version": "0.1"},
        })

    def list_tools(self) -> list[dict]:
        result = self._call("tools/list", {})
        return result.get("tools", []) if result else []

    def call_tool(self, name: str, arguments: dict) -> tuple[str, bool]:
        """Returns (content_text, is_error)."""
        result = self._call("tools/call", {"name": name, "arguments": arguments})
        if not result:
            return "", True
        content = result.get("content", [])
        text    = "\n".join(c.get("text", "") for c in content if c.get("type") == "text")
        return text, result.get("isError", False)


class InlineMCPClient:
    """
    Calls the MCP dispatcher directly — no HTTP round-trip.
    Used in tests and when running eval inside the same process.

    manifest: ToolManifest.to_dict()
    proxy_fn: optional callable(tool_name, args) -> str for real upstream calls
    """

    def __init__(self, manifest: dict, proxy_fn=None):
        self._tools: dict[str, dict] = {
            t["name"]: t for t in manifest.get("tools", [])
        }
        self._proxy = proxy_fn
        self._title = manifest.get("title", "")

    def initialize(self) -> dict:
        return {
            "protocolVersion": "2024-11-05",
            "serverInfo": {"name": self._title},
            "capabilities": {"tools": {}},
        }

    def list_tools(self) -> list[dict]:
        from plugfit.server.protocol import make_tool_schema
        return [make_tool_schema(t) for t in self._tools.values()]

    def call_tool(self, name: str, arguments: dict) -> tuple[str, bool]:
        if name not in self._tools:
            return f"Tool not found: {name}", True

        tool = self._tools[name]

        # Check required params
        missing = [
            k for k, p in tool.get("parameters", {}).items()
            if p.get("required") and k not in arguments
        ]
        if missing:
            return f"Missing required params: {missing}", True

        if self._proxy:
            try:
                result = self._proxy(name, arguments)
                return result, False
            except Exception as e:
                return str(e), True

        # No proxy — return a realistic stub
        return json.dumps({
            "ok":      True,
            "tool":    name,
            "args":    arguments,
            "note":    "stub response — no upstream configured",
        }), False
