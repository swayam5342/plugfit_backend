import json
from typing import Any
import urllib
from .tool_handlers import get_tool_handler


class ProxyError(Exception):
    pass


def _build_openapi_request(
    method: str,
    path_template: str,
    args: dict[str, Any],
    base_url: str,
    headers: dict[str, str],
    tool_raw: dict | None = None,
) -> tuple[str, dict, dict]:
    import re
    import urllib.parse

    param_locations = {}
    if tool_raw and "operation" in tool_raw:
        for param in tool_raw["operation"].get("parameters", []):
            param_name = param["name"]
            param_in = param.get("in", "query")
            param_locations[param_name] = param_in

    path = path_template
    path_param_names = set(re.findall(r"\{(\w+)\}", path_template))

    query_params = {}
    body_params = {}
    header_params = {}

    for k, v in args.items():
        if param_locations.get(k) == "header":
            header_params[k] = str(v)
        elif k in path_param_names or param_locations.get(k) == "path":
            encoded_value = urllib.parse.quote(str(v), safe="")
            path = path.replace(f"{{{k}}}", encoded_value)
        # Check if this is a query parameter
        elif param_locations.get(k) == "query":
            query_params[k] = v
        else:
            body_params[k] = v
    if header_params:
        headers.update(header_params)

    url = base_url.rstrip("/") + path
    if query_params:
        query_string = urllib.parse.urlencode(query_params, doseq=True)
        url = f"{url}?{query_string}"
    if method.upper() in ("GET", "DELETE", "HEAD"):
        if body_params:
            query_string = urllib.parse.urlencode(body_params, doseq=True)
            url = f"{url}&{query_string}" if "?" in url else f"{url}?{query_string}"
        return url, {}, {}
    return url, query_params, body_params


def call_tool(
    tool: dict,
    args: dict[str, Any],
    base_url: str,
    extra_headers: dict[str, str] | None = None,
    timeout: int = 15,
) -> str:
    if handler := get_tool_handler(tool["name"]):
        result = handler(args)
        return json.dumps(result) if not isinstance(result, str) else result

    import urllib.parse

    headers: dict[str, str] = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "PlugFit/0.1",
    }
    if extra_headers:
        headers.update(extra_headers)

    method = tool.get("http_method") or "POST"
    http_path = tool.get("http_path") or f"/{tool['name']}"
    source = tool.get("source", "unknown")
    tool_raw = tool.get("raw")  # Get the raw OpenAPI data

    if source == "openapi" and http_path:
        url, _, body = _build_openapi_request(
            method,
            http_path,
            args,
            base_url,
            headers,
            tool_raw,
        )
    else:
        url = base_url.rstrip("/") + f"/{tool['name']}"
        body = args
        method = "POST"

    body_bytes = json.dumps(body).encode() if body else None

    req = urllib.request.Request(  # type:ignore
        url,
        data=body_bytes,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # type:ignore
            raw = resp.read().decode("utf-8", errors="replace")
            return raw
    except urllib.error.HTTPError as e:  # type:ignore
        body_text = e.read().decode("utf-8", errors="replace")
        raise ProxyError(f"HTTP {e.code} {e.reason} from {url}: {body_text[:300]}")
    except urllib.error.URLError as e:  # type:ignore
        raise ProxyError(f"Network error reaching {url}: {e.reason}")
    except Exception as e:
        raise ProxyError(f"Unexpected error calling {url}: {e}")


class MockProxy:
    def __init__(self):
        self._registry: dict[str, Any] = {}
        self._call_log: list[dict] = []

    def register(self, tool_name: str, response: Any):
        """Pre-load a fake response for a given tool."""
        self._registry[tool_name] = response

    def call(self, tool: dict, args: dict[str, Any]) -> str:
        name = tool["name"]
        self._call_log.append({"tool": name, "args": args})
        if name in self._registry:
            result = self._registry[name]
            return json.dumps(result) if not isinstance(result, str) else result
        # Default: echo back what was sent
        return json.dumps({"ok": True, "tool": name, "received": args})

    @property
    def call_log(self) -> list[dict]:
        return list(self._call_log)

    def reset(self):
        self._call_log.clear()
