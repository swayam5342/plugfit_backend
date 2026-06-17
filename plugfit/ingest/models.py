from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass
class ToolParam:
    type: str  # "string" | "integer" | "boolean" | "object" | "array"
    description: str = ""
    required: bool = False
    enum: list[str] = field(default_factory=list)
    default: Any = None
    items: dict = field(default_factory=dict)  # for array types
    properties: dict = field(default_factory=dict)  # for object types

    def to_dict(self) -> dict:
        d = {"type": self.type, "required": self.required}
        if self.description:
            d["description"] = self.description
        if self.enum:
            d["enum"] = self.enum
        if self.default is not None:
            d["default"] = self.default
        if self.items:
            d["items"] = self.items
        if self.properties:
            d["properties"] = self.properties
        return d


@dataclass
class CanonicalTool:
    name: str
    description: str
    parameters: dict[str, ToolParam]
    source: Literal["openapi", "mcp", "unknown"] = "unknown"
    raw: dict = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    http_method: str = ""
    http_path: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": {k: v.to_dict() for k, v in self.parameters.items()},
            "source": self.source,
            "tags": self.tags,
            "http_method": self.http_method,
            "http_path": self.http_path,
            "raw": self.raw,
        }


@dataclass
class ToolManifest:
    tools: list[CanonicalTool]
    source_type: Literal["openapi", "mcp", "unknown"] = "unknown"
    source_uri: str = ""  # file path or URL
    title: str = ""
    version: str = ""
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "version": self.version,
            "source_type": self.source_type,
            "source_uri": self.source_uri,
            "tool_count": len(self.tools),
            "warnings": self.warnings,
            "tools": [t.to_dict() for t in self.tools],
        }

    def summary(self) -> str:
        lines = [
            f"  Source : {self.source_type} — {self.source_uri or '(inline)'}",
            f"  Title  : {self.title or '(untitled)'}",
            f"  Tools  : {len(self.tools)}",
        ]
        if self.warnings:
            lines.append(f"  Warns  : {len(self.warnings)}")
            for w in self.warnings:
                lines.append(f"           ! {w}")
        return "\n".join(lines)
