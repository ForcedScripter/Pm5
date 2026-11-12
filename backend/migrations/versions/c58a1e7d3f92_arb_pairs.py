"""arb_pairs: Pair Arbitrage paper purchases

Revision ID: c58a1e7d3f92
Revises: b27f4c9d1e55
Create Date: 2026-11-11

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'c58a1e7d3f92'
down_revision: Union[str, Sequence[str], None] = 'b27f4c9d1e55'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

MONEY = sa.Numeric(precision=18, scale=6)
PRICE = sa.Numeric(precision=10, scale=6)
TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        'arb_pairs',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('user_id', sa.UUID(), nullable=False),
        sa.Column('strategy_config_id', sa.UUID(), nullable=False),
        sa.Column('market_id', sa.UUID(), nullable=False),
        sa.Column('asset', sa.String(length=10), nullable=False),
        sa.Column('mode', postgresql.ENUM('paper', 'live', name='trading_mode', create_type=False), nullable=False),
        sa.Column('status', sa.String(length=8), nullable=False),
        sa.Column('shares', MONEY, nullable=False),
        sa.Column('up_price', PRICE, nullable=False),
        sa.Column('down_price', PRICE, nullable=False),
        sa.Column('cost', MONEY, nullable=False),
        sa.Column('fees', MONEY, nullable=False),
        sa.Column('locked_pnl', MONEY, nullable=False),
        sa.Column('pnl', MONEY, nullable=True),
        sa.Column('diag', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('window_end', TS, nullable=False),
        sa.Column('opened_at', TS, nullable=False),
        sa.Column('settled_at', TS, nullable=True),
        sa.Column('created_at', TS, server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['strategy_config_id'], ['strategy_configurations.id']),
        sa.ForeignKeyConstraint(['market_id'], ['markets.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_arb_pairs_user_mode_status', 'arb_pairs', ['user_id', 'mode', 'status'])


def downgrade() -> None:
    op.drop_index('ix_arb_pairs_user_mode_status', table_name='arb_pairs')
    op.drop_table('arb_pairs')
