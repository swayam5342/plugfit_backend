"""add server.score_method

Records which scorer ("eval" | "heuristic") produced score_before/score_after
so the headline delta is never a mix of two different rulers.

Revision ID: b3f1c8a90d21
Revises: a2227b51763d
Create Date: 2026-07-27

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b3f1c8a90d21'
down_revision: Union[str, Sequence[str], None] = 'a2227b51763d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'servers',
        sa.Column('score_method', sa.String(length=20), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('servers', 'score_method')
