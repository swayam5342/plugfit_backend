from __future__ import annotations

import json
import logging
import random
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

log = logging.getLogger("plugfit.eval.mock_server")


# ── Stub response generator ───────────────────────────────────────────────────


def _sample_value(pname: str, spec: Any) -> Any:
    """Synthesise a plausible value for a declared parameter from its schema."""
    if not isinstance(spec, dict):
        return f"sample_{pname}"
    if spec.get("enum"):
        return spec["enum"][0]
    return {
        "string": f"sample_{pname}",
        "integer": 1,
        "number": 1.0,
        "boolean": False,
        "array": [],
        "object": {},
    }.get(spec.get("type", "string"), f"sample_{pname}")


def _stub_response(tool: dict, args: dict[str, Any]) -> dict:
    """
    Generate a plausible stub response for any tool call, derived ONLY from
    the tool's own metadata (http method/path, declared parameter schema,
    generic verb prefix) — never from specific tool names or any domain.
    The agent sees this and uses it to form its final answer.
    """
    name = tool["name"]
    params = tool.get("parameters", {}) or {}
    method = (tool.get("http_method") or "").upper()
    path = tool.get("http_path", "")

    # A record shaped like the tool's own schema: echo provided args,
    # synthesise the rest of the declared fields.
    record = {p: args.get(p, _sample_value(p, spec)) for p, spec in params.items()}

    # If any param is named 'id' or ends with '_id', echo it back
    ids = {k: v for k, v in args.items() if k == "id" or k.endswith("_id")}

    verb = name.split("_", 1)[0].lower()

    if method == "POST" or verb in ("create", "add", "insert", "post"):
        return {"id": random.randint(100, 999), "created": True, **record}
    if method == "DELETE" or verb in ("delete", "remove"):
        return {"deleted": True, **(ids or record)}
    if method in ("PUT", "PATCH") or verb in ("update", "patch", "mark", "toggle", "set"):
        return {"updated": True, **(ids or record)}
    if verb in ("search", "find", "query"):
        return {
            "results": [record] if record else [],
            "total": 1 if record else 0,
            "query": args.get("query", args.get("q", "")),
        }
    if method == "GET" or verb in ("get", "list", "fetch", "read"):
        # Single resource when addressed by id (arg or path template),
        # otherwise a collection.
        if ids or "{" in path:
            return {"item": record or dict(ids)}
        return {"items": [record] if record else [{}], "count": 1}

    # Fallback
    return {"ok": True, "tool": name, "args": args}


# ── Request handler ───────────────────────────────────────────────────────────


class _MCPHandler(BaseHTTPRequestHandler):
    """Handles MCP JSON-RPC POST requests."""

    server: "MockMCPServer"  # type: ignore[assignment]

    def log_message(self, *args):
        pass  # suppress access logs

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)

        try:
            msg = json.loads(body)
        except json.JSONDecodeError:
            self._respond(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": "Parse error"},
                }
            )
            return

        method = msg.get("method", "")
        id_ = msg.get("id")
        params = msg.get("params") or {}

        if method == "initialize":
            self._respond(
                {
                    "jsonrpc": "2.0",
                    "id": id_,
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "serverInfo": {"name": self.server.title, "version": "0.0.1"},
                        "capabilities": {"tools": {}},
                    },
                }
            )

        elif method == "tools/list":
            schemas = [self._tool_schema(t) for t in self.server.tools.values()]
            self._respond({"jsonrpc": "2.0", "id": id_, "result": {"tools": schemas}})

        elif method == "tools/call":
            name = params.get("name", "")
            args = params.get("arguments") or {}
            tool = self.server.tools.get(name)

            if not tool:
                self._respond(
                    {
                        "jsonrpc": "2.0",
                        "id": id_,
                        "error": {"code": -32001, "message": f"Tool not found: {name}"},
                    }
                )
                return

            # Log the call
            self.server.call_log.append({"tool": name, "args": args})

            result_text = json.dumps(_stub_response(tool, args))
            self._respond(
                {
                    "jsonrpc": "2.0",
                    "id": id_,
                    "result": {
                        "content": [{"type": "text", "text": result_text}],
                        "isError": False,
                    },
                }
            )

        elif method == "ping":
            self._respond({"jsonrpc": "2.0", "id": id_, "result": {}})

        else:
            self._respond(
                {
                    "jsonrpc": "2.0",
                    "id": id_,
                    "error": {"code": -32601, "message": f"Method not found: {method}"},
                }
            )

    def _respond(self, data: dict):
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    @staticmethod
    def _tool_schema(tool: dict) -> dict:
        props = {}
        required = []
        for pname, p in tool.get("parameters", {}).items():
            prop: dict = {"type": p.get("type", "string")}
            if p.get("description"):
                prop["description"] = p["description"]
            if p.get("enum"):
                prop["enum"] = p["enum"]
            props[pname] = prop
            if p.get("required"):
                required.append(pname)
        return {
            "name": tool["name"],
            "description": tool.get("description", ""),
            "inputSchema": {
                "type": "object",
                "properties": props,
                **({"required": required} if required else {}),
            },
        }


class MockMCPServer:
    """
    Runs an MCP server on localhost:PORT in a background thread.
    Use as a context manager.

        with MockMCPServer(manifest) as srv:
            print(srv.url)     # http://localhost:PORT
            print(srv.call_log)  # list of {tool, args} after eval
    """

    def __init__(self, manifest: dict):
        self.title = manifest.get("title", "mock-server")
        self.tools: dict[str, dict] = {t["name"]: t for t in manifest.get("tools", [])}
        self.call_log: list[dict] = []
        self._httpd: HTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.port: int = 0
        self.url: str = ""

    def __enter__(self) -> "MockMCPServer":
        # Bind on port 0 → OS picks a free port
        self._httpd = HTTPServer(("127.0.0.1", 0), _MCPHandler)
        self._httpd.server = self  # inject reference for handler
        self.port = self._httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        log.info("MockMCPServer started on %s (%d tools)", self.url, len(self.tools))
        return self

    def __exit__(self, *_):
        if self._httpd:
            self._httpd.shutdown()
        log.info("MockMCPServer stopped. %d calls logged.", len(self.call_log))

    def reset_log(self):
        self.call_log.clear()
