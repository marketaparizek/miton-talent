"""portfolio open roles: companies, runs, per-company run outcomes, roles

Revision ID: c2d3e4f5a6b7
Revises: b1c2d3e4f5a6
Create Date: 2026-09-30

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c2d3e4f5a6b7'
down_revision: Union[str, Sequence[str], None] = 'b1c2d3e4f5a6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'portfolio_companies',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('slug', sa.String(length=64), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('stage', sa.String(length=32), nullable=True),
        sa.Column('not_on_site', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('group_name', sa.String(length=64), nullable=True),
        sa.Column('website', sa.String(length=500), nullable=True),
        sa.Column('careers_url', sa.String(length=500), nullable=True),
        sa.Column('adapter', sa.String(length=32), nullable=True),
        sa.Column('config', sa.JSON(), nullable=False),
        sa.Column('cats', sa.JSON(), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('active', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('slug'),
    )
    op.create_table(
        'portfolio_runs',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('actor', sa.String(length=255), nullable=True),
        sa.Column('companies_total', sa.Integer(), nullable=False),
        sa.Column('companies_failed', sa.Integer(), nullable=False),
        sa.Column('roles_total', sa.Integer(), nullable=False),
        sa.Column('roles_new', sa.Integer(), nullable=False),
        sa.Column('roles_closed', sa.Integer(), nullable=False),
        sa.Column('error', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_portfolio_runs_started_at', 'portfolio_runs', ['started_at'])
    op.create_table(
        'portfolio_company_runs',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('run_id', sa.Integer(), nullable=False),
        sa.Column('company_id', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(length=24), nullable=False),
        sa.Column('adapter', sa.String(length=32), nullable=True),
        sa.Column('source_url', sa.String(length=500), nullable=True),
        sa.Column('http_status', sa.Integer(), nullable=True),
        sa.Column('roles_found', sa.Integer(), nullable=False),
        sa.Column('raw_rows', sa.Integer(), nullable=False),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('duration_ms', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['company_id'], ['portfolio_companies.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['run_id'], ['portfolio_runs.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('run_id', 'company_id', name='uq_portfolio_company_run'),
    )
    op.create_index('ix_portfolio_company_runs_run_id', 'portfolio_company_runs', ['run_id'])
    op.create_index('ix_portfolio_company_runs_company_id', 'portfolio_company_runs', ['company_id'])
    op.create_table(
        'portfolio_roles',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('company_id', sa.Integer(), nullable=False),
        sa.Column('fingerprint', sa.String(length=255), nullable=False),
        sa.Column('title', sa.String(length=500), nullable=False),
        sa.Column('location', sa.String(length=255), nullable=True),
        sa.Column('locations', sa.JSON(), nullable=False),
        sa.Column('team', sa.String(length=255), nullable=True),
        sa.Column('employment_type', sa.String(length=64), nullable=True),
        sa.Column('url', sa.String(length=1000), nullable=True),
        sa.Column('fn', sa.String(length=32), nullable=True),
        sa.Column('fn_source', sa.String(length=16), nullable=True),
        sa.Column('is_technical', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('cc', sa.String(length=8), nullable=True),
        sa.Column('first_seen_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('first_seen_run_id', sa.Integer(), nullable=True),
        sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_seen_run_id', sa.Integer(), nullable=True),
        sa.Column('closed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('closed_run_id', sa.Integer(), nullable=True),
        sa.Column('raw', sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(['company_id'], ['portfolio_companies.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('company_id', 'fingerprint', 'first_seen_run_id', name='uq_portfolio_role_run'),
    )
    op.create_index('ix_portfolio_roles_company_id', 'portfolio_roles', ['company_id'])
    op.create_index('ix_portfolio_roles_company_closed', 'portfolio_roles', ['company_id', 'closed_at'])
    op.create_index('ix_portfolio_roles_fn', 'portfolio_roles', ['fn'])
    op.create_index('ix_portfolio_roles_first_seen_run_id', 'portfolio_roles', ['first_seen_run_id'])
    op.create_index('ix_portfolio_roles_last_seen_run_id', 'portfolio_roles', ['last_seen_run_id'])
    op.create_index('ix_portfolio_roles_closed_at', 'portfolio_roles', ['closed_at'])
    op.create_index('ix_portfolio_roles_closed_run_id', 'portfolio_roles', ['closed_run_id'])


def downgrade() -> None:
    op.drop_table('portfolio_roles')
    op.drop_table('portfolio_company_runs')
    op.drop_index('ix_portfolio_runs_started_at', table_name='portfolio_runs')
    op.drop_table('portfolio_runs')
    op.drop_table('portfolio_companies')
