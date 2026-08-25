"""
Project routes.

POST   /projects/               — create a project
GET    /projects/                — list current tenant's projects
GET    /projects/{id}           — one project + its servers
PATCH  /projects/{id}            — rename a project
DELETE /projects/{id}            — delete a project (servers are orphaned,
                                    not deleted — project_id set to NULL)
"""

import logging
import secrets

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from plugfit.app.db.db import get_db
from plugfit.app.models.models import Project, User
from plugfit.app.routes.auth import get_current_user
from plugfit.app.routes.server import _get_server_for_tenant, _server_out
from plugfit.app.schema.project import (
    ProjectCreate,
    ProjectDetailOut,
    ProjectOut,
    ProjectUpdate,
)
from plugfit.app.schema.server import ServerOut
from plugfit.app.utils.auth.make_slug import make_slug

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/projects", tags=["projects"])


async def _get_project_for_tenant(
    project_id: str, tenant: User, db: AsyncSession, *, with_servers: bool = False
) -> Project:
    """Fetch project by id, enforcing tenant ownership."""
    stmt = select(Project).where(
        Project.id == project_id, Project.user_id == tenant.id
    )
    if with_servers:
        stmt = stmt.options(selectinload(Project.servers))
    result = await db.execute(stmt)
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


async def _unique_project_slug(
    name: str, tenant_id: str, db: AsyncSession, *, exclude_id: str | None = None
) -> str:
    slug = make_slug(name)
    stmt = select(Project).where(
        Project.user_id == tenant_id, Project.slug == slug
    )
    if exclude_id:
        stmt = stmt.where(Project.id != exclude_id)
    result = await db.execute(stmt)
    if result.scalar_one_or_none():
        slug = f"{slug}-{secrets.token_hex(4)}"
    return slug


@router.post(
    "/",
    response_model=ProjectOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a project",
)
async def create_project(
    payload: ProjectCreate,
    tenant: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProjectOut:
    logger.info(f"Project creation request: name='{payload.name}', user_id={tenant.id}")
    slug = await _unique_project_slug(payload.name, tenant.id, db)
    project = Project(user_id=tenant.id, name=payload.name, slug=slug)
    db.add(project)
    await db.commit()
    await db.refresh(project)
    logger.info(f"Project created successfully: {payload.name} (ID: {project.id})")
    return _project_out(project, server_count=0)


@router.get(
    "/",
    response_model=list[ProjectOut],
    summary="List all projects for the current user",
)
async def list_projects(
    tenant: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[ProjectOut]:
    logger.debug(f"Listing projects for user_id: {tenant.id}")
    result = await db.execute(
        select(Project)
        .where(Project.user_id == tenant.id)
        .options(selectinload(Project.servers))
        .order_by(Project.created_at.desc())
    )
    projects = [
        _project_out(p, server_count=len(p.servers)) for p in result.scalars().all()
    ]
    logger.debug(f"Found {len(projects)} projects for user_id: {tenant.id}")
    return projects


@router.get(
    "/{project_id}",
    response_model=ProjectDetailOut,
    summary="Get a single project with its servers",
)
async def get_project(
    project_id: str,
    tenant: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProjectDetailOut:
    logger.debug(f"Fetching project: {project_id}, user_id: {tenant.id}")
    project = await _get_project_for_tenant(project_id, tenant, db, with_servers=True)
    return ProjectDetailOut(
        **_project_out(project, server_count=len(project.servers)).model_dump(),
        servers=[_server_out(s) for s in project.servers],
    )


@router.patch(
    "/{project_id}",
    response_model=ProjectOut,
    summary="Rename a project",
)
async def update_project(
    project_id: str,
    payload: ProjectUpdate,
    tenant: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProjectOut:
    logger.info(f"Project rename request: {project_id}, user_id: {tenant.id}")
    project = await _get_project_for_tenant(project_id, tenant, db, with_servers=True)
    slug = await _unique_project_slug(
        payload.name, tenant.id, db, exclude_id=project.id
    )
    project.name = payload.name
    project.slug = slug
    await db.commit()
    await db.refresh(project)
    logger.info(f"Project renamed successfully: {project_id} -> {payload.name}")
    return _project_out(project, server_count=len(project.servers))


@router.delete(
    "/{project_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a project (servers are orphaned, not deleted)",
)
async def delete_project(
    project_id: str,
    tenant: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    logger.info(f"Delete project request: {project_id}, user_id: {tenant.id}")
    project = await _get_project_for_tenant(project_id, tenant, db)
    await db.delete(project)
    await db.commit()
    logger.info(f"Project deleted successfully: {project_id}")


@router.post(
    "/{project_id}/servers/{server_id}",
    response_model=ServerOut,
    summary="Attach a server to a project",
)
async def attach_server(
    project_id: str,
    server_id: str,
    tenant: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ServerOut:
    logger.info(
        f"Attach server request: project={project_id}, server={server_id}, user_id={tenant.id}"
    )
    project = await _get_project_for_tenant(project_id, tenant, db)
    server = await _get_server_for_tenant(server_id, tenant, db)
    server.project_id = project.id
    await db.commit()
    await db.refresh(server)
    logger.info(f"Server attached: server={server_id} -> project={project_id}")
    return _server_out(server)


@router.delete(
    "/{project_id}/servers/{server_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Detach a server from a project",
)
async def detach_server(
    project_id: str,
    server_id: str,
    tenant: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    logger.info(
        f"Detach server request: project={project_id}, server={server_id}, user_id={tenant.id}"
    )
    project = await _get_project_for_tenant(project_id, tenant, db)
    server = await _get_server_for_tenant(server_id, tenant, db)
    if server.project_id != project.id:
        raise HTTPException(
            status_code=404, detail="Server is not attached to this project"
        )
    server.project_id = None
    await db.commit()
    logger.info(f"Server detached: server={server_id} from project={project_id}")


def _project_out(project: Project, *, server_count: int) -> ProjectOut:
    return ProjectOut(
        id=project.id,
        tenant_id=project.user_id,
        name=project.name,
        slug=project.slug,
        server_count=server_count,
        created_at=project.created_at,
        updated_at=project.updated_at,
    )
