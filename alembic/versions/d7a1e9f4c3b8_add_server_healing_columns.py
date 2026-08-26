"""add server healing columns (healed_manifest, score_healed, healing_meta)

Phase 6 / F-05 self-healing loop. cleaned_manifest/raw_manifest are NEVER
overwritten by healing — healed_manifest is a separate, optional artifact.
No auto-promotion in this build: MCP serving continues to read
cleaned_manifest exactly as before this migration.

Revision ID: d7a1e9f4c3b8
Revises: c599be8835b2
Create Date: 2026-08-26

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd7a1e9f4c3b8'
down_revision: Union[str, Sequence[str], None] = 'c599be8835b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('servers', sa.Column('healed_manifest', sa.JSON(), nullable=True))
    op.add_column('servers', sa.Column('score_healed', sa.Float(), nullable=True))
    op.add_column('servers', sa.Column('healing_meta', sa.JSON(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('servers', 'healing_meta')
    op.drop_column('servers', 'score_healed')
    op.drop_column('servers', 'healed_manifest')
