"""audit fixes: admin sessions, server-side chat, shared rate counters, job locks, consent version

Revision ID: d3e4f5a6b7c8
Revises: c2d3e4f5a6b7
Create Date: 2026-10-22

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd3e4f5a6b7c8'
down_revision: Union[str, Sequence[str], None] = 'c2d3e4f5a6b7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'admin_sessions',
        sa.Column('id', sa.String(length=64), nullable=False),
        sa.Column('user_id', sa.String(length=64), nullable=False),
        sa.Column('email', sa.String(length=255), nullable=False),
        sa.Column('role', sa.String(length=16), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_admin_sessions_email', 'admin_sessions', ['email'])
    op.create_index('ix_admin_sessions_expires_at', 'admin_sessions', ['expires_at'])

    op.create_table(
        'chat_conversations',
        sa.Column('id', sa.String(length=32), nullable=False),
        sa.Column('lang', sa.String(length=8), nullable=False),
        sa.Column('turns', sa.JSON(), nullable=False),
        sa.Column('user_turns', sa.Integer(), nullable=False),
        sa.Column('profile', sa.JSON(), nullable=False),
        sa.Column('summary', sa.Text(), nullable=True),
        sa.Column('stage', sa.String(length=32), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_chat_conversations_updated_at', 'chat_conversations', ['updated_at'])

    op.create_table(
        'rate_counters',
        sa.Column('key', sa.String(length=128), nullable=False),
        sa.Column('window_start', sa.Integer(), nullable=False),
        sa.Column('count', sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint('key', 'window_start'),
    )

    op.create_table(
        'job_locks',
        sa.Column('name', sa.String(length=64), nullable=False),
        sa.Column('holder', sa.String(length=64), nullable=False),
        sa.Column('acquired_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('name'),
    )

    op.add_column('candidates', sa.Column('consent_version', sa.String(length=32), nullable=True))


def downgrade() -> None:
    op.drop_column('candidates', 'consent_version')
    op.drop_table('job_locks')
    op.drop_table('rate_counters')
    op.drop_index('ix_chat_conversations_updated_at', table_name='chat_conversations')
    op.drop_table('chat_conversations')
    op.drop_index('ix_admin_sessions_expires_at', table_name='admin_sessions')
    op.drop_index('ix_admin_sessions_email', table_name='admin_sessions')
    op.drop_table('admin_sessions')
