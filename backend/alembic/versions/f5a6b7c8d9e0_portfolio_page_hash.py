"""portfolio company runs: page fingerprint and model answer, so an unchanged page skips the model

Revision ID: f5a6b7c8d9e0
Revises: e4f5a6b7c8d9
Create Date: 2026-10-27

Written off d3e4f5a6b7c8 next to the audit migration; rechained after it
(e4f5a6b7c8d9, already on main and prod) on 22 Nov 2026 so there is one head.

Additive only, every column nullable. Existing rows keep NULL, so the first
run after the deploy reads every plain page with the model as before and
stores the fingerprints; reuse starts the week after.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f5a6b7c8d9e0'
down_revision: Union[str, Sequence[str], None] = 'e4f5a6b7c8d9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('portfolio_company_runs') as t:
        t.add_column(sa.Column('page_hash', sa.String(length=64), nullable=True))
        t.add_column(sa.Column('extracted', sa.JSON(), nullable=True))
        t.add_column(sa.Column('reused_from_run_id', sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('portfolio_company_runs') as t:
        for col in ('reused_from_run_id', 'extracted', 'page_hash'):
            t.drop_column(col)
