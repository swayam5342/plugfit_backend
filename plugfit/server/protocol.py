from typing import Any


def ok(id_: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def err(id_: Any, code: int, message: str, data: Any = None) -> dict:
    e = {"code": code, "message": message}
    if data is not None:
        e["data"] = data
    return {"jsonrpc": "2.0", "id": id_, "error": e}


PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
TOOL_NOT_FOUND = -32001
TOOL_EXEC_ERROR = -32002


SERVER_INFO = {
    "name": "plugfit-server",
    "version": "0.1.0",
}

CAPABILITIES = {
    "tools": {"listChanged": False},
}


def make_initialize_result(server_name: str = "") -> dict:
    info = dict(SERVER_INFO)
    if server_name:
        info["name"] = server_name
    return {
        "protocolVersion": "2024-11-05",
        "serverInfo": info,
        "capabilities": CAPABILITIES,
    }


def make_tool_schema(tool: dict) -> dict:
    properties: dict = {}
    required: list[str] = []

    for pname, p in tool.get("parameters", {}).items():
        prop: dict = {"type": p.get("type", "string")}
        if p.get("description"):
            prop["description"] = p["description"]
        if p.get("enum"):
            prop["enum"] = p["enum"]
        if p.get("default") is not None:
            prop["default"] = p["default"]
        if p.get("items"):
            prop["items"] = p["items"]
        if p.get("properties"):
            prop["properties"] = p["properties"]
        properties[pname] = prop
        if p.get("required"):
            required.append(pname)

    return {
        "name": tool["name"],
        "description": tool.get("description", ""),
        "inputSchema": {
            "type": "object",
            "properties": properties,
            **({"required": required} if required else {}),
        },
    }


def make_tool_result(content: str, is_error: bool = False) -> dict:
    return {
        "content": [{"type": "text", "text": content}],
        "isError": is_error,
    }
