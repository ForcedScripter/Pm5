"""strike_markets + strike_positions: crypto strike markets on paper

Revision ID: 9c41d7e2a6b3
Revises: 7a3c2e9f1b40
Create Date: 2026-11-11

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '9c41d7e2a6b3'
down_revision: Union[str, Sequence[str], None] = '7a3c2e9f1b40'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

MONEY = sa.Numeric(precision=18, scale=6)
PRICE = sa.Numeric(precision=10, scale=6)
JSONB = postgresql.JSONB(astext_type=sa.Text())
TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        'strike_markets',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('slug', sa.String(length=200), nullable=False),
        sa.Column('event_slug', sa.String(length=200), nullable=False),
        sa.Column('question', sa.Text(), nullable=False),
        sa.Column('asset', sa.String(length=10), nullable=False),
        sa.Column('kind', sa.String(length=8), nullable=False),
        sa.Column('strike', MONEY, nullable=False),
        sa.Column('range_start', TS, nullable=True),
        sa.Column('expiry', TS, nullable=False),
        sa.Column('condition_id', sa.String(length=80), nullable=False),
        sa.Column('yes_token', sa.String(length=100), nullable=False),
        sa.Column('no_token', sa.String(length=100), nullable=False),
        sa.Column('tick_size', PRICE, nullable=False),
        sa.Column('min_order_size', MONEY, nullable=False),
        sa.Column('fee_rate', PRICE, nullable=False),
        sa.Column('closed', sa.Boolean(), nullable=False),
        sa.Column('winner', sa.String(length=3), nullable=True),
        sa.Column('own_winner', sa.String(length=3), nullable=True),
        sa.Column('own_detail', JSONB, nullable=False),
        sa.Column('samples', JSONB, nullable=False),
        sa.Column('resolved_at', TS, nullable=True),
        sa.Column('created_at', TS, server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('slug'),
    )
    op.create_index('ix_strike_markets_event_slug', 'strike_markets', ['event_slug'])
    op.create_index('ix_strike_markets_open', 'strike_markets', ['closed', 'expiry'])
    op.create_table(
        'strike_positions',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('user_id', sa.UUID(), nullable=False),
        sa.Column('strategy_config_id', sa.UUID(), nullable=False),
        sa.Column('strike_market_id', sa.UUID(), nullable=False),
        sa.Column('mode', postgresql.ENUM('paper', 'live', name='trading_mode', create_type=False), nullable=False),
        sa.Column('side', sa.String(length=3), nullable=False),
        sa.Column('status', sa.String(length=8), nullable=False),
        sa.Column('shares', MONEY, nullable=False),
        sa.Column('entry_price', PRICE, nullable=False),
        sa.Column('cost', MONEY, nullable=False),
        sa.Column('model_prob', PRICE, nullable=False),
        sa.Column('mark', PRICE, nullable=True),
        sa.Column('exit_price', PRICE, nullable=True),
        sa.Column('fees', MONEY, nullable=False),
        sa.Column('pnl', MONEY, nullable=True),
        sa.Column('diag', JSONB, nullable=False),
        sa.Column('opened_at', TS, nullable=False),
        sa.Column('closed_at', TS, nullable=True),
        sa.Column('created_at', TS, server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['strategy_config_id'], ['strategy_configurations.id']),
        sa.ForeignKeyConstraint(['strike_market_id'], ['strike_markets.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'mode', 'strike_market_id', name='uq_strike_position_market'),
    )
    op.create_index('ix_strike_positions_user_mode_status', 'strike_positions', ['user_id', 'mode', 'status'])


def downgrade() -> None:
    op.drop_index('ix_strike_positions_user_mode_status', table_name='strike_positions')
    op.drop_table('strike_positions')
    op.drop_index('ix_strike_markets_open', table_name='strike_markets')
    op.drop_index('ix_strike_markets_event_slug', table_name='strike_markets')
    op.drop_table('strike_markets')
