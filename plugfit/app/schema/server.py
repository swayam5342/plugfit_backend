from datetime import datetime
from pydantic import BaseModel, Field


class ServerCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    base_url: str | None = Field(
        None,
        description="Upstream API base URL for proxying real tool calls",
        max_length=500,
    )
    upstream_headers: dict[str, str] | None = Field(
        None,
        description="Auth headers forwarded to upstream (e.g. Authorization)",
    )


class ServerOut(BaseModel):
    id: str
    tenant_id: str
    project_id: str | None = None
    name: str
    slug: str
    status: str
    spec_source: str
    score_before: float | None
    score_after: float | None
    score_method: str | None = None
    tool_count_before: int | None
    tool_count_after: int | None
    base_url: str | None
    mcp_path: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ScoreSummary(BaseModel):
    """Compact score card — used by the dashboard."""

    server_id: str
    name: str
    score_before: float | None
    score_after: float | None
    score_method: str | None = None
    delta: float | None
    tool_count_before: int | None
    tool_count_after: int | None
    status: str


class JobOut(BaseModel):
    id: str
    server_id: str
    status: str
    stage: str
    logs: list[dict]
    error: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ToolDiff(BaseModel):
    name: str
    description_before: str | None
    description_after: str | None
    param_count: int
    changed: bool


class ManifestDiff(BaseModel):
    server_id: str
    tools_before: int
    tools_after: int
    tools_removed: list[str]
    tools_merged: list[str]
    diffs: list[ToolDiff]
