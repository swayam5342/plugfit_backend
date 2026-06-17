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
    name: str
    slug: str
    status: str
    spec_source: str
    score_before: float | None
    score_after: float | None
    tool_count_before: int | None
    tool_count_after: int | None
    base_url: str | None
    mcp_path: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
