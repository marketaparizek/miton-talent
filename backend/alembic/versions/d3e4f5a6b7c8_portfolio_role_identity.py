"""portfolio roles: identity by board id, title history, relisted and merged postings

Revision ID: d3e4f5a6b7c8
Revises: c2d3e4f5a6b7
Create Date: 2026-10-26

Existing rows keep role_key NULL. The next run adopts each of them by its
fingerprint, so the deploy itself produces no false openings or closures.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd3e4f5a6b7c8'
down_revision: Union[str, Sequence[str], None] = 'c2d3e4f5a6b7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('portfolio_roles') as t:
        t.add_column(sa.Column('role_key', sa.String(length=600), nullable=True))
        t.add_column(sa.Column('key_tier', sa.String(length=8), nullable=True))
        t.add_column(sa.Column('external_ids', sa.JSON(), nullable=False, server_default='[]'))
        t.add_column(sa.Column('title_history', sa.JSON(), nullable=False, server_default='[]'))
        t.add_column(sa.Column('replaces_role_id', sa.Integer(), nullable=True))
        t.add_column(sa.Column('first_seen_original_at', sa.DateTime(timezone=True), nullable=True))
        t.add_column(sa.Column('relist_reason', sa.String(length=32), nullable=True))
        t.add_column(sa.Column('merged_into_role_id', sa.Integer(), nullable=True))
        t.create_index('ix_portfolio_roles_role_key', ['role_key'])


def downgrade() -> None:
    with op.batch_alter_table('portfolio_roles') as t:
        t.drop_index('ix_portfolio_roles_role_key')
        for col in ('merged_into_role_id', 'relist_reason', 'first_seen_original_at', 'replaces_role_id',
                    'title_history', 'external_ids', 'key_tier', 'role_key'):
            t.drop_column(col)
