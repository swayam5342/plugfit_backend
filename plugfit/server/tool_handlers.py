from __future__ import annotations

from typing import Any, Callable

ToolHandler = Callable[[dict[str, Any]], Any]

_handlers: dict[str, ToolHandler] = {}


def register_tool_handler(name: str, handler: ToolHandler) -> None:
    """Register a custom handler for a pure MCP tool."""
    _handlers[name] = handler


def get_tool_handler(name: str) -> ToolHandler | None:
    return _handlers.get(name)


def clear_tool_handlers() -> None:
    _handlers.clear()
