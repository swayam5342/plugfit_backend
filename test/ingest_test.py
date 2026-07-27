"""
Tests for PlugFit ingest stage.
Run with: python -m pytest test_ingest.py -v
or:        python test_ingest.py
"""

import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from plugfit.ingest.ingest import ingest, ToolManifest
from plugfit.ingest.models import CanonicalTool


# ── Fixtures ──────────────────────────────────────────────────────────────────

OPENAPI3_PETSTORE = {
    "openapi": "3.0.0",
    "info": {"title": "Petstore", "version": "1.0.0"},
    "paths": {
        "/pets": {
            "get": {
                "operationId": "listPets",
                "summary": "List all pets",
                "tags": ["pets"],
                "parameters": [
                    {
                        "name": "limit",
                        "in": "query",
                        "required": False,
                        "schema": {"type": "integer", "default": 10},
                        "description": "Max number of results",
                    }
                ],
                "responses": {"200": {"description": "OK"}},
            },
            "post": {
                "operationId": "createPet",
                "summary": "Create a pet",
                "description": "Creates a new pet in the store",
                "tags": ["pets"],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["name"],
                                "properties": {
                                    "name": {
                                        "type": "string",
                                        "description": "Pet name",
                                    },
                                    "tag": {
                                        "type": "string",
                                        "description": "Optional tag",
                                    },
                                },
                            }
                        }
                    },
                },
                "responses": {"201": {"description": "Created"}},
            },
        },
        "/pets/{petId}": {
            "get": {
                "operationId": "showPetById",
                "summary": "Info for a specific pet",
                "parameters": [
                    {
                        "name": "petId",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                        "description": "The id of the pet to retrieve",
                    }
                ],
                "responses": {"200": {"description": "OK"}},
            },
            "delete": {
                # No description — should generate a warning
                "operationId": "deletePet",
                "parameters": [
                    {
                        "name": "petId",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                    }
                ],
                "responses": {"204": {"description": "Deleted"}},
            },
        },
    },
}

SWAGGER2_SPEC = {
    "swagger": "2.0",
    "info": {"title": "Simple API", "version": "0.1"},
    "paths": {
        "/users": {
            "get": {
                "operationId": "getUsers",
                "summary": "List users",
                "description": "Returns a list of all users in the system",
                "parameters": [
                    {
                        "name": "active",
                        "in": "query",
                        "type": "boolean",
                        "required": False,
                        "description": "Filter by active status",
                    }
                ],
                "responses": {"200": {"description": "OK"}},
            }
        }
    },
}

MCP_TOOLS_LIST = {
    "result": {
        "serverInfo": {"name": "github-mcp"},
        "tools": [
            {
                "name": "create_issue",
                "description": "Creates a new issue in a GitHub repository. Use this when the user wants to file a bug report or feature request.",
                "inputSchema": {
                    "type": "object",
                    "required": ["owner", "repo", "title"],
                    "properties": {
                        "owner": {
                            "type": "string",
                            "description": "Repository owner (user or org)",
                        },
                        "repo": {"type": "string", "description": "Repository name"},
                        "title": {"type": "string", "description": "Issue title"},
                        "body": {
                            "type": "string",
                            "description": "Issue body (markdown)",
                        },
                        "labels": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Labels to apply",
                        },
                    },
                },
            },
            {
                "name": "list_issues",
                "description": "List issues",  # too short — should warn
                "inputSchema": {
                    "type": "object",
                    "required": ["owner", "repo"],
                    "properties": {
                        "owner": {"type": "string"},
                        "repo": {"type": "string"},
                        "state": {"type": "string", "enum": ["open", "closed", "all"]},
                    },
                },
            },
            {
                "name": "list_issues",  # duplicate — should warn
                "description": "Get issues from a repo",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "owner": {"type": "string"},
                        "repo": {"type": "string"},
                    },
                },
            },
            {
                "name": "no_description_tool",
                # missing description entirely — should warn
                "inputSchema": {
                    "type": "object",
                    "properties": {"x": {"type": "string"}},
                },
            },
        ],
    }
}

BARE_TOOLS_LIST = [
    {
        "name": "send_email",
        "description": "Sends an email to one or more recipients via the configured SMTP server.",
        "inputSchema": {
            "type": "object",
            "required": ["to", "subject"],
            "properties": {
                "to": {"type": "string", "description": "Recipient email"},
                "subject": {"type": "string", "description": "Email subject"},
                "body": {"type": "string", "description": "Email body"},
            },
        },
    }
]


# ── Test helpers ──────────────────────────────────────────────────────────────

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"
results = []


def check(label: str, condition: bool, detail: str = ""):
    status = PASS if condition else FAIL
    msg = f"  [{status}] {label}"
    if detail:
        msg += f" — {detail}"
    print(msg)
    results.append(condition)
    return condition


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_openapi3():
    print("\n── OpenAPI 3.x (Petstore) ──")
    m = ingest(OPENAPI3_PETSTORE)
    print(m)

    check("Returns ToolManifest", isinstance(m, ToolManifest))
    check("source_type = openapi", m.source_type == "openapi")
    check("title parsed", m.title == "Petstore")
    check("version parsed", m.version == "1.0.0")
    check("4 tools extracted", len(m.tools) == 4, f"got {len(m.tools)}")

    tool_names = {t.name for t in m.tools}
    check(
        "listPets present",
        "listpets" in tool_names
        or "list_pets" in tool_names
        or "listpets" in tool_names,
    )
    check("createPet present", "createpet" in tool_names or "create_pet" in tool_names)

    create = next(t for t in m.tools if "create" in t.name.lower())
    check("createPet has name param", "name" in create.parameters)
    check("name param is required", create.parameters["name"].required)
    check(
        "tag param is not required",
        not create.parameters.get("tag", CanonicalTool("", "", {})).required
        if "tag" in create.parameters
        else True,
    )
    check("createPet has description", len(create.description) > 0)
    check("http_method = POST", create.http_method == "POST")
    check("http_path set", create.http_path == "/pets")

    list_tool = next(t for t in m.tools if "list" in t.name.lower())
    check("listPets has limit param", "limit" in list_tool.parameters)
    check("limit has default", list_tool.parameters["limit"].default == 10)
    check("limit not required", not list_tool.parameters["limit"].required)

    delete = next(t for t in m.tools if "delete" in t.name.lower())
    check(
        "deletePet produces warning (no description)",
        any("delete" in w.lower() for w in m.warnings),
    )

    print(f"  Warnings: {m.warnings}")


def test_swagger2():
    print("\n── Swagger 2.x ──")
    m = ingest(SWAGGER2_SPEC)
    print(m)

    check("Returns ToolManifest", isinstance(m, ToolManifest))
    check("source_type = openapi", m.source_type == "openapi")
    check("1 tool extracted", len(m.tools) == 1, f"got {len(m.tools)}")

    t = m.tools[0]
    check(
        "getUsers tool name",
        "getusers" in t.name.lower() or "get_users" in t.name.lower(),
    )
    check("active param parsed", "active" in t.parameters)
    check("active param type = boolean", t.parameters["active"].type == "boolean")
    check("active not required", not t.parameters["active"].required)


def test_mcp_response():
    print("\n── MCP tools/list response ──")
    m = ingest(MCP_TOOLS_LIST)
    print(m)

    check("Returns ToolManifest", isinstance(m, ToolManifest))
    check("source_type = mcp", m.source_type == "mcp")
    check("title from serverInfo", m.title == "github-mcp")
    check("4 tools extracted", len(m.tools) == 4, f"got {len(m.tools)}")

    create = next(t for t in m.tools if t.name == "create_issue")
    check("create_issue found", create is not None)
    check("owner required", create.parameters["owner"].required)
    check("body not required", not create.parameters["body"].required)
    check("labels is array type", create.parameters["labels"].type == "array")

    list_t = next(t for t in m.tools if t.name == "list_issues")
    check(
        "state has enum", list_t.parameters["state"].enum == ["open", "closed", "all"]
    )

    check("duplicate warning raised", any("duplicate" in w.lower() for w in m.warnings))
    check(
        "empty description warning raised",
        any("no_description" in w for w in m.warnings),
    )
    check(
        "short description warning raised",
        any("list_issues" in w and "short" in w.lower() for w in m.warnings),
    )

    print(f"  Warnings: {m.warnings}")


def test_bare_list():
    print("\n── Bare MCP tools list ──")
    m = ingest(BARE_TOOLS_LIST)
    print(m)

    check("Returns ToolManifest", isinstance(m, ToolManifest))
    check("source_type = mcp", m.source_type == "mcp")
    check("1 tool", len(m.tools) == 1)
    t = m.tools[0]
    check("to param required", t.parameters["to"].required)
    check("body not required", not t.parameters["body"].required)


def test_file_roundtrip(tmp_path=None):
    print("\n── File roundtrip (JSON + YAML) ──")
    import tempfile, pathlib

    spec = OPENAPI3_PETSTORE

    with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
        json.dump(spec, f)
        json_path = f.name

    with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
        import yaml

        yaml.dump(spec, f)
        yaml_path = f.name

    try:
        m_json = ingest(json_path)
        check("JSON file: 4 tools", len(m_json.tools) == 4, f"got {len(m_json.tools)}")

        m_yaml = ingest(yaml_path)
        check("YAML file: 4 tools", len(m_yaml.tools) == 4, f"got {len(m_yaml.tools)}")
        check("YAML title matches", m_yaml.title == "Petstore")
    finally:
        os.unlink(json_path)
        os.unlink(yaml_path)


def test_to_dict():
    print("\n── Serialisation (to_dict) ──")
    m = ingest(MCP_TOOLS_LIST)
    print(m)
    d = m.to_dict()

    check("tool_count key", d["tool_count"] == 4)
    check("tools is list", isinstance(d["tools"], list))
    check("each tool has name", all("name" in t for t in d["tools"]))
    check("each tool has parameters", all("parameters" in t for t in d["tools"]))
    check("raw preserved", all("raw" in t for t in d["tools"]))


# ── Runner ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Make plugfit importable
    sys.path.insert(0, "/home/claude")

    # Rename package dir to be importable
    import importlib, types

    test_openapi3()
    test_swagger2()
    test_mcp_response()
    test_bare_list()
    test_file_roundtrip()
    test_to_dict()

    passed = sum(results)
    total = len(results)
    print(f"\n{'─' * 40}")
    print(f"  {passed}/{total} checks passed")
    if passed == total:
        print("  \033[92mAll good.\033[0m")
    else:
        print(f"  \033[91m{total - passed} failed.\033[0m")
        sys.exit(1)
