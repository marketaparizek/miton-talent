"""provider_costs: what Anthropic, OpenAI, Bright Data and Hetzner billed, for /admin/usage

Revision ID: b7c8d9e0f1a2
Revises: a6b7c8d9e0f1
Create Date: 2026-11-22

A new table, nothing else touched.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b7c8d9e0f1a2'
down_revision: Union[str, Sequence[str], None] = 'a6b7c8d9e0f1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'provider_costs',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('provider', sa.String(length=32), nullable=False),
        sa.Column('source', sa.String(length=16), nullable=False),
        sa.Column('day', sa.Date(), nullable=False),
        sa.Column('scope', sa.String(length=128), nullable=False),
        sa.Column('amount_micro', sa.BigInteger(), nullable=False),
        sa.Column('currency', sa.String(length=3), nullable=False),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_by', sa.String(length=255), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('provider', 'source', 'day', 'scope', name='uq_provider_cost'),
    )
    op.create_index('ix_provider_costs_day', 'provider_costs', ['day'])


def downgrade() -> None:
    op.drop_index('ix_provider_costs_day', table_name='provider_costs')
    op.drop_table('provider_costs')
