import json
import sys
import traceback
from typing import Any

from .protocol import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    TOOL_NOT_FOUND,
    err,
    make_initialize_result,
    make_tool_result,
    make_tool_schema,
    ok,
)
from .proxy import MockProxy, ProxyError, call_tool
from .tool_handlers import get_tool_handler


def log(msg: str):
    """Write a log line to stderr — never touches stdout."""
    print(f"[plugfit] {msg}", file=sys.stderr, flush=True)


class MCPServer:
    def __init__(
        self,
        manifest: dict,
        base_url: str = "",
        extra_headers: dict[str, str] | None = None,
        mock_proxy: MockProxy | None = None,
    ):
        self.manifest = manifest
        self.base_url = base_url
        self.extra_headers = extra_headers or {}
        self.mock_proxy = mock_proxy
        self.initialized = False

        # Index tools by name for O(1) lookup
        self._tools: dict[str, dict] = {t["name"]: t for t in manifest.get("tools", [])}

        title = manifest.get("title") or "plugfit-server"
        log(f"Loaded {len(self._tools)} tools from '{title}'")

    def handle(self, message: dict) -> dict | None:
        id_ = message.get("id")
        method = message.get("method", "")
        params = message.get("params") or {}
        if id_ is None and method in ("notifications/initialized",):
            return None
        try:
            if method == "initialize":
                return self._handle_initialize(id_, params)
            elif method == "tools/list":
                return self._handle_tools_list(id_, params)
            elif method == "tools/call":
                return self._handle_tools_call(id_, params)
            elif method == "ping":
                return ok(id_, {})
            else:
                return err(id_, METHOD_NOT_FOUND, f"Method not found: {method}")
        except Exception as e:
            log(f"Unhandled exception in {method}: {e}\n{traceback.format_exc()}")
            return err(id_, INTERNAL_ERROR, str(e))

    def _handle_initialize(self, id_: Any, params: dict) -> dict:
        client_info = params.get("clientInfo", {})
        log(
            f"Client connected: {client_info.get('name', 'unknown')} {client_info.get('version', '')}"
        )
        self.initialized = True
        title = self.manifest.get("title", "")
        return ok(id_, make_initialize_result(server_name=title or "plugfit-server"))

    def _handle_tools_list(self, id_: Any, params: dict) -> dict:
        schemas = [make_tool_schema(t) for t in self._tools.values()]
        log(f"tools/list → {len(schemas)} tools")
        return ok(id_, {"tools": schemas})

    def _handle_tools_call(self, id_: Any, params: dict) -> dict:
        name = params.get("name", "")
        args = params.get("arguments") or {}

        if name not in self._tools:
            log(f"tools/call '{name}' — not found")
            return err(id_, TOOL_NOT_FOUND, f"Tool not found: '{name}'")

        tool = self._tools[name]
        log(f"tools/call '{name}' args={list(args.keys())}")

        # Validate required params
        missing = [
            pname
            for pname, p in tool.get("parameters", {}).items()
            if p.get("required") and pname not in args
        ]
        if missing:
            return err(
                id_,
                INVALID_PARAMS,
                f"Missing required parameters for '{name}': {missing}",
            )
        try:
            if self.mock_proxy:
                result_text = self.mock_proxy.call(tool, args)
            elif self.base_url or get_tool_handler(name):
                result_text = call_tool(tool, args, self.base_url, self.extra_headers)
            else:
                result_text = json.dumps(
                    {
                        "note": "No base_url configured — this is a stub response",
                        "tool": name,
                        "received_args": args,
                    }
                )

            log(f"tools/call '{name}' → ok ({len(result_text)} chars)")
            return ok(id_, make_tool_result(result_text))

        except ProxyError as e:
            log(f"tools/call '{name}' → proxy error: {e}")
            return ok(id_, make_tool_result(str(e), is_error=True))

    def run(self):
        log("Server ready — listening on stdin")

        for raw_line in sys.stdin:
            raw_line = raw_line.strip()
            if not raw_line:
                continue

            # Parse incoming JSON
            try:
                message = json.loads(raw_line)
            except json.JSONDecodeError as e:
                response = err(None, PARSE_ERROR, f"Invalid JSON: {e}")
                self._send(response)
                continue

            # Dispatch
            response = self.handle(message)
            if response is not None:
                self._send(response)

        log("stdin closed — shutting down")

    def _send(self, response: dict):
        line = json.dumps(response, separators=(",", ":"))
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
