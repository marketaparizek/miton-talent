"""used_handoff_tokens: replay protection for the Alister sign-in handoff

Revision ID: b1c2d3e4f5a6
Revises: a0c6f94ba5a9
Create Date: 2026-09-30

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b1c2d3e4f5a6'
down_revision: Union[str, Sequence[str], None] = 'a0c6f94ba5a9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'used_handoff_tokens',
        sa.Column('jti', sa.String(length=64), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('used_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('jti'),
    )
    op.create_index('ix_used_handoff_tokens_expires_at', 'used_handoff_tokens', ['expires_at'])


def downgrade() -> None:
    op.drop_index('ix_used_handoff_tokens_expires_at', table_name='used_handoff_tokens')
    op.drop_table('used_handoff_tokens')
