"""maker_rounds: Spork Maker paper rounds

Revision ID: 7a3c2e9f1b40
Revises: 1f675f74dc27
Create Date: 2026-11-06

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '7a3c2e9f1b40'
down_revision: Union[str, Sequence[str], None] = '1f675f74dc27'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

MONEY = sa.Numeric(precision=18, scale=6)


def upgrade() -> None:
    op.create_table(
        'maker_rounds',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('user_id', sa.UUID(), nullable=False),
        sa.Column('strategy_config_id', sa.UUID(), nullable=False),
        sa.Column('market_id', sa.UUID(), nullable=False),
        sa.Column('asset', sa.String(length=10), nullable=False),
        sa.Column('mode', postgresql.ENUM('paper', 'live', name='trading_mode', create_type=False), nullable=False),
        sa.Column('status', sa.String(length=12), nullable=False),
        sa.Column('sets', MONEY, nullable=False),
        sa.Column('up_left', MONEY, nullable=False),
        sa.Column('down_left', MONEY, nullable=False),
        sa.Column('proceeds', MONEY, nullable=False),
        sa.Column('merged_sets', MONEY, nullable=True),
        sa.Column('residual_side', postgresql.ENUM('UP', 'DOWN', name='outcome_side', create_type=False), nullable=True),
        sa.Column('residual_shares', MONEY, nullable=True),
        sa.Column('redeem_value', MONEY, nullable=True),
        sa.Column('pnl', MONEY, nullable=True),
        sa.Column('rebate_est', MONEY, nullable=False),
        sa.Column('edge', sa.Numeric(precision=10, scale=6), nullable=False),
        sa.Column('fills', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('diag', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('window_end', sa.DateTime(timezone=True), nullable=False),
        sa.Column('settled_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['strategy_config_id'], ['strategy_configurations.id']),
        sa.ForeignKeyConstraint(['market_id'], ['markets.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'mode', 'market_id', name='uq_maker_round_market'),
    )
    op.create_index('ix_maker_rounds_user_mode_status', 'maker_rounds', ['user_id', 'mode', 'status'])


def downgrade() -> None:
    op.drop_index('ix_maker_rounds_user_mode_status', table_name='maker_rounds')
    op.drop_table('maker_rounds')
