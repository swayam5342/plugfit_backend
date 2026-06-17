"""
PlugFit Ingest — public API.

Single function: ingest(source) → ToolManifest

source can be:
  - A file path string  (.json, .yaml, .yml)
  - A URL string        (http/https — MCP server or remote spec)
  - A dict              (pre-loaded spec or MCP response)
  - A list              (bare MCP tools array)

Detection order:
  1. List                → MCP tools array
  2. Dict with 'openapi' or 'swagger' key → OpenAPI spec
  3. Dict with 'tools' or 'result' key   → MCP response
  4. URL string ending in /mcp or with json-rpc hint → live MCP fetch
  5. URL string          → fetch & detect
  6. File path .yaml/.yml/.json          → load & detect
"""

import json
from pathlib import Path
from typing import Union

import yaml

from plugfit.ingest.mcp import (
    fetch_and_parse_mcp,
    parse_mcp_response,
    parse_mcp_tools_list,
)
from plugfit.ingest.openapi import parse_openapi_dict
from .models import ToolManifest


# ── Content detection ─────────────────────────────────────────────────────────
def _detect_dict(data: dict) -> str:
    """Return 'openapi' or 'mcp' based on dict keys."""
    if "openapi" in data or "swagger" in data:
        return "openapi"
    if "paths" in data and "info" in data:
        return "openapi"
    if "tools" in data or ("result" in data and isinstance(data.get("result"), dict)):
        return "mcp"
    if "jsonrpc" in data:
        return "mcp"
    if "name" in data and ("inputSchema" in data or "input_schema" in data):
        return "mcp_single"
    return "unknown"


def _is_mcp_url(url: str) -> bool:
    lower = url.lower()
    return any(
        hint in lower for hint in ["/mcp", "/sse", "mcp-server", "modelcontextprotocol"]
    )


def _load_file(path: str) -> tuple[Union[dict, list], str]:
    """Load a JSON or YAML file. Returns (data, file_uri)."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"File not found: {path}")

    text = p.read_text(encoding="utf-8")
    suffix = p.suffix.lower()

    if suffix in (".yaml", ".yml"):
        data = yaml.safe_load(text)
    elif suffix == ".json":
        data = json.loads(text)
    else:
        # Try JSON first, fall back to YAML
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = yaml.safe_load(text)

    return data, p.as_uri()


# ── URL fetching ──────────────────────────────────────────────────────────────


def _fetch_url(url: str) -> tuple[Union[dict, list], str]:
    """Fetch a URL and parse as JSON or YAML."""
    try:
        import httpx
    except ImportError:
        raise RuntimeError("httpx required for URL fetching: pip install httpx")

    resp = httpx.get(url, timeout=15, follow_redirects=True)
    resp.raise_for_status()

    content_type = resp.headers.get("content-type", "")
    text = resp.text

    if "yaml" in content_type or url.endswith((".yaml", ".yml")):
        data = yaml.safe_load(text)
    else:
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = yaml.safe_load(text)

    return data, url


# ── Main ingest function ──────────────────────────────────────────────────────


def ingest(source: Union[str, dict, list], source_hint: str = "") -> ToolManifest:
    """
    Universal ingest entry point.

    Args:
        source:      File path, URL, dict, or list — see module docstring.
        source_hint: Optional 'openapi' or 'mcp' to skip auto-detection.

    Returns:
        ToolManifest with canonical tools ready for the cleaning stage.
    """

    # ── List: bare MCP tools array ─────────────────────────────────────────
    if isinstance(source, list):
        return parse_mcp_tools_list(source, source_uri="(inline list)")

    # ── Dict: pre-loaded spec or response ─────────────────────────────────
    if isinstance(source, dict):
        kind = source_hint or _detect_dict(source)
        if kind == "openapi":
            return parse_openapi_dict(source, source_uri="(inline dict)")
        elif kind == "mcp_single":
            return parse_mcp_tools_list([source], source_uri="(inline single tool)")
        else:
            return parse_mcp_response(source, source_uri="(inline dict)")

    # ── String: file path or URL ───────────────────────────────────────────
    if not isinstance(source, str):
        raise TypeError(f"source must be str, dict, or list — got {type(source)}")

    source = source.strip()

    # Live MCP server fetch (URL that looks like an MCP endpoint)
    if source.startswith(("http://", "https://")) and (
        _is_mcp_url(source) or source_hint == "mcp"
    ):
        return fetch_and_parse_mcp(source)

    # Remote file (URL but not detected as MCP)
    if source.startswith(("http://", "https://")):
        data, uri = _fetch_url(source)
        if isinstance(data, list):
            return parse_mcp_tools_list(data, source_uri=uri)
        kind = source_hint or _detect_dict(data)
        if kind == "openapi":
            return parse_openapi_dict(data, source_uri=uri)
        return parse_mcp_response(data, source_uri=uri)

    # Local file
    data, uri = _load_file(source)
    if isinstance(data, list):
        return parse_mcp_tools_list(data, source_uri=uri)
    kind = source_hint or _detect_dict(data)
    if kind == "openapi":
        return parse_openapi_dict(data, source_uri=uri)
    return parse_mcp_response(data, source_uri=uri)
