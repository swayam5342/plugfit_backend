import enum
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.ext.mutable import MutableDict, MutableList
from sqlalchemy.orm import Mapped, mapped_column, relationship

from plugfit.app.db.db import Base
from plugfit.app.utils.db import new_uuid, new_api_key, utcnow


class JobStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class ServerStatus(str, enum.Enum):
    UPLOADING = "uploading"
    PROCESSING = "processing"
    READY = "ready"
    ERROR = "error"


class SpecSource(str, enum.Enum):
    OPENAPI = "openapi"
    MCP = "mcp"
    UNKNOWN = "unknown"


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=new_uuid,
    )

    name: Mapped[str] = mapped_column(
        String(120),
        nullable=False,
    )

    email: Mapped[str] = mapped_column(
        String(254),
        unique=True,
        nullable=False,
        index=True,
    )

    slug: Mapped[str] = mapped_column(
        String(40),
        unique=True,
        nullable=False,
        index=True,
    )

    password_hash: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
    )

    # not sure if i want to keep it
    api_key: Mapped[str] = mapped_column(
        String(64),
        unique=True,
        nullable=False,
        default=new_api_key,
        index=True,
    )

    plan: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="free",
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
    )

    servers: Mapped[list["Server"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def __repr__(self) -> str:
        return f"<User {self.email}>"


class Server(Base):
    __tablename__ = "servers"

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "slug",
            name="uq_server_user_slug",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=new_uuid,
    )

    user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    name: Mapped[str] = mapped_column(
        String(120),
        nullable=False,
    )

    slug: Mapped[str] = mapped_column(
        String(60),
        nullable=False,
    )

    status: Mapped[ServerStatus] = mapped_column(
        Enum(ServerStatus),
        nullable=False,
        default=ServerStatus.UPLOADING,
    )

    spec_source: Mapped[SpecSource] = mapped_column(
        Enum(SpecSource),
        nullable=False,
        default=SpecSource.UNKNOWN,
    )

    raw_spec: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    raw_manifest: Mapped[dict | None] = mapped_column(
        MutableDict.as_mutable(JSON),
        nullable=True,
    )

    cleaned_manifest: Mapped[dict | None] = mapped_column(
        MutableDict.as_mutable(JSON),
        nullable=True,
    )

    score_before: Mapped[float | None] = mapped_column(
        Float,
        nullable=True,
    )

    score_after: Mapped[float | None] = mapped_column(
        Float,
        nullable=True,
    )

    tool_count_before: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )

    tool_count_after: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )

    base_url: Mapped[str | None] = mapped_column(
        String(500),
        nullable=True,
    )

    upstream_headers: Mapped[dict | None] = mapped_column(
        MutableDict.as_mutable(JSON),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
    )

    user: Mapped["User"] = relationship(
        back_populates="servers",
    )

    jobs: Mapped[list["Job"]] = relationship(
        back_populates="server",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="Job.created_at",
    )

    @property
    def mcp_path(self) -> str:
        return f"/mcp/{self.user.slug}/{self.slug}"

    def __repr__(self) -> str:
        return f"<Server {self.slug}>"


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=new_uuid,
    )

    server_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("servers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus),
        nullable=False,
        default=JobStatus.PENDING,
    )

    stage: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
        default="pending",
    )

    logs: Mapped[list] = mapped_column(
        MutableList.as_mutable(JSON),
        nullable=False,
        default=list,
    )

    error: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
    )

    server: Mapped["Server"] = relationship(
        back_populates="jobs",
    )

    def __repr__(self) -> str:
        return f"<Job {self.id} {self.status.value}>"
