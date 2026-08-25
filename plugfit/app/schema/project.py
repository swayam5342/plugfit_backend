from datetime import datetime
from pydantic import BaseModel, Field

from plugfit.app.schema.server import ServerOut


class ProjectCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)


class ProjectUpdate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)


class ProjectOut(BaseModel):
    id: str
    tenant_id: str
    name: str
    slug: str
    server_count: int
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ProjectDetailOut(ProjectOut):
    servers: list[ServerOut]
