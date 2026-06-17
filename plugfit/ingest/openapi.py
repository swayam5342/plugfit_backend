import re
from .models import CanonicalTool, ToolManifest, ToolParam


def _slugify(text: str) -> str:
    """Turn arbitrary text into a valid snake_case tool name."""
    text = re.sub(r"[^a-zA-Z0-9]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("_")
    return text.lower()


def _resolve_ref(ref: str, spec: dict) -> dict:
    """Resolve a $ref like '#/components/schemas/Foo' within the same doc."""
    if not ref.startswith("#/"):
        return {}
    parts = ref.lstrip("#/").split("/")
    node = spec
    for p in parts:
        if not isinstance(node, dict):
            return {}
        node = node.get(p, {})
    return node if isinstance(node, dict) else {}


def _schema_to_param(schema: dict, required: bool, spec: dict) -> ToolParam:
    """Convert a JSON Schema fragment to a ToolParam."""
    if "$ref" in schema:
        schema = _resolve_ref(schema["$ref"], spec)

    ptype = schema.get("type", "string")
    # JSON Schema allows type arrays — take first concrete type
    if isinstance(ptype, list):
        ptype = next((t for t in ptype if t != "null"), "string")

    return ToolParam(
        type=ptype,
        description=schema.get("description", ""),
        required=required,
        enum=schema.get("enum", []),
        default=schema.get("default"),
        items=schema.get("items", {}),
        properties=schema.get("properties", {}),
    )


# ── OpenAPI 3.x ──────────────────────────────────────────────────────────────
def _parse_openapi3_operation(
    method: str,
    path: str,
    operation: dict,
    path_item: dict,
    spec: dict,
    warnings: list[str],
) -> CanonicalTool:
    op_id = operation.get("operationId", "")
    name = _slugify(op_id) if op_id else _slugify(f"{method}_{path}")

    description = (
        operation.get("description") or operation.get("summary") or ""
    ).strip()

    if not description:
        warnings.append(
            f"{name}: no description or summary — agent will struggle to pick this tool"
        )

    params: dict[str, ToolParam] = {}

    # Path-level + operation-level parameters (operation overrides path-level)
    all_params: dict[str, dict] = {}
    for p in path_item.get("parameters", []):
        if "$ref" in p:
            p = _resolve_ref(p["$ref"], spec)
        all_params[p.get("name", "")] = p
    for p in operation.get("parameters", []):
        if "$ref" in p:
            p = _resolve_ref(p["$ref"], spec)
        all_params[p.get("name", "")] = p

    for pname, p in all_params.items():
        if not pname:
            continue
        schema = p.get("schema", {"type": "string"})
        required = p.get("required", p.get("in") == "path")
        param = _schema_to_param(schema, required, spec)
        param.description = param.description or p.get("description", "")
        params[pname] = param

    # Request body — flatten top-level properties into params
    body = operation.get("requestBody", {})
    if body:
        content = body.get("content", {})
        media = (
            content.get("application/json")
            or content.get("application/x-www-form-urlencoded")
            or next(iter(content.values()), {})
        )
        body_schema = media.get("schema", {})
        if "$ref" in body_schema:
            body_schema = _resolve_ref(body_schema["$ref"], spec)

        body_required = set(body_schema.get("required", []))
        for prop_name, prop_schema in body_schema.get("properties", {}).items():
            if prop_name not in params:
                param = _schema_to_param(prop_schema, prop_name in body_required, spec)
                params[prop_name] = param

    return CanonicalTool(
        name=name,
        description=description,
        parameters=params,
        source="openapi",
        raw={"method": method, "path": path, "operation": operation},
        tags=operation.get("tags", []) + [method.upper()],
        http_method=method.upper(),
        http_path=path,
    )


def parse_openapi3(spec: dict, source_uri: str = "") -> ToolManifest:
    info = spec.get("info", {})
    warnings: list[str] = []
    tools: list[CanonicalTool] = []

    for path, path_item in spec.get("paths", {}).items():
        if "$ref" in path_item:
            path_item = _resolve_ref(path_item["$ref"], spec)
        for method in ["get", "post", "put", "patch", "delete", "head", "options"]:
            operation = path_item.get(method)
            if not isinstance(operation, dict):
                continue
            tool = _parse_openapi3_operation(
                method, path, operation, path_item, spec, warnings
            )
            tools.append(tool)

    return ToolManifest(
        tools=tools,
        source_type="openapi",
        source_uri=source_uri,
        title=info.get("title", ""),
        version=info.get("version", ""),
        warnings=warnings,
    )


# ── Swagger 2.x ──────────────────────────────────────────────────────────────


def _swagger2_schema_to_param(param: dict, spec: dict) -> ToolParam:
    """Swagger 2 mixes schema fields directly into parameter objects."""
    schema = param.get("schema", param)  # body params have 'schema'; others are inline
    return _schema_to_param(schema, param.get("required", False), spec)


def parse_swagger2(spec: dict, source_uri: str = "") -> ToolManifest:
    info = spec.get("info", {})
    warnings: list[str] = []
    tools: list[CanonicalTool] = []

    for path, path_item in spec.get("paths", {}).items():
        for method in ["get", "post", "put", "patch", "delete"]:
            operation = path_item.get(method)
            if not isinstance(operation, dict):
                continue

            op_id = operation.get("operationId", "")
            name = _slugify(op_id) if op_id else _slugify(f"{method}_{path}")
            description = (
                operation.get("description") or operation.get("summary") or ""
            ).strip()

            if not description:
                warnings.append(f"{name}: no description — agent will struggle")

            params: dict[str, ToolParam] = {}
            for p in operation.get("parameters", []):
                if "$ref" in p:
                    p = _resolve_ref(p["$ref"], spec)
                pname = p.get("name", "")
                if not pname:
                    continue
                tp = _swagger2_schema_to_param(p, spec)
                tp.description = tp.description or p.get("description", "")
                params[pname] = tp

            tools.append(
                CanonicalTool(
                    name=name,
                    description=description,
                    parameters=params,
                    source="openapi",
                    raw={"method": method, "path": path, "operation": operation},
                    tags=operation.get("tags", []) + [method.upper()],
                    http_method=method.upper(),
                    http_path=path,
                )
            )

    return ToolManifest(
        tools=tools,
        source_type="openapi",
        source_uri=source_uri,
        title=info.get("title", ""),
        version=info.get("version", ""),
        warnings=warnings,
    )


# ── Entry point ───────────────────────────────────────────────────────────────
def parse_openapi_dict(spec: dict, source_uri: str = "") -> ToolManifest:
    """Route to the right parser based on spec version field."""
    if "openapi" in spec:
        return parse_openapi3(spec, source_uri)
    elif "swagger" in spec:
        return parse_swagger2(spec, source_uri)
    else:
        # Best-effort: try 3.x parser and warn
        manifest = parse_openapi3(spec, source_uri)
        manifest.warnings.insert(
            0,
            "No 'openapi' or 'swagger' version field found — parsed as OpenAPI 3.x best-effort",
        )
        return manifest
