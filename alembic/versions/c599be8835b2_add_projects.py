"""add projects table + server.project_id

User -> Project -> Server grouping layer. Project is optional per server
(server.project_id nullable, ondelete=SET NULL) — no backfill needed,
existing servers stay project-less.

Revision ID: c599be8835b2
Revises: b3f1c8a90d21
Create Date: 2026-08-25

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c599be8835b2'
down_revision: Union[str, Sequence[str], None] = 'b3f1c8a90d21'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'projects',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('user_id', sa.String(length=36), nullable=False),
        sa.Column('name', sa.String(length=120), nullable=False),
        sa.Column('slug', sa.String(length=60), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'slug', name='uq_project_user_slug'),
    )
    op.create_index(op.f('ix_projects_user_id'), 'projects', ['user_id'])

    op.add_column(
        'servers',
        sa.Column('project_id', sa.String(length=36), nullable=True),
    )
    op.create_index(op.f('ix_servers_project_id'), 'servers', ['project_id'])
    op.create_foreign_key(
        'fk_servers_project_id_projects',
        'servers', 'projects',
        ['project_id'], ['id'],
        ondelete='SET NULL',
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint('fk_servers_project_id_projects', 'servers', type_='foreignkey')
    op.drop_index(op.f('ix_servers_project_id'), table_name='servers')
    op.drop_column('servers', 'project_id')

    op.drop_index(op.f('ix_projects_user_id'), table_name='projects')
    op.drop_table('projects')
