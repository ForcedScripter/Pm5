"""weather_events, weather_metars, weather_samples: weather data collection (read only)

Revision ID: b27f4c9d1e55
Revises: 9c41d7e2a6b3
Create Date: 2026-11-11

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'b27f4c9d1e55'
down_revision: Union[str, Sequence[str], None] = '9c41d7e2a6b3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSONB = postgresql.JSONB(astext_type=sa.Text())
TS = sa.DateTime(timezone=True)
TEMP = sa.Numeric(precision=6, scale=1)


def upgrade() -> None:
    op.create_table(
        'weather_events',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('slug', sa.String(length=200), nullable=False),
        sa.Column('city', sa.String(length=60), nullable=False),
        sa.Column('station', sa.String(length=8), nullable=False),
        sa.Column('unit', sa.String(length=1), nullable=False),
        sa.Column('local_date', sa.Date(), nullable=False),
        sa.Column('end_date', TS, nullable=True),
        sa.Column('utc_offset_s', sa.Integer(), nullable=True),
        sa.Column('buckets', JSONB, nullable=False),
        sa.Column('closed', sa.Boolean(), nullable=False),
        sa.Column('winner_label', sa.String(length=40), nullable=True),
        sa.Column('own_max_c', TEMP, nullable=True),
        sa.Column('own_reports', sa.Integer(), nullable=True),
        sa.Column('own_label', sa.String(length=40), nullable=True),
        sa.Column('resolved_at', TS, nullable=True),
        sa.Column('created_at', TS, server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('slug'),
    )
    op.create_index('ix_weather_events_city_date', 'weather_events', ['city', 'local_date'])
    op.create_table(
        'weather_metars',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('station', sa.String(length=8), nullable=False),
        sa.Column('obs_time', TS, nullable=False),
        sa.Column('temp_c', TEMP, nullable=False),
        sa.Column('speci', sa.Boolean(), nullable=False),
        sa.Column('raw', sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('station', 'obs_time', name='uq_weather_metar_station_time'),
    )
    op.create_table(
        'weather_samples',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('event_id', sa.UUID(), nullable=False),
        sa.Column('ts', TS, nullable=False),
        sa.Column('running_max_c', TEMP, nullable=True),
        sa.Column('reports', sa.Integer(), nullable=False),
        sa.Column('forecast', JSONB, nullable=False),
        sa.Column('ladder', JSONB, nullable=False),
        sa.ForeignKeyConstraint(['event_id'], ['weather_events.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_weather_samples_event_ts', 'weather_samples', ['event_id', 'ts'])


def downgrade() -> None:
    op.drop_index('ix_weather_samples_event_ts', table_name='weather_samples')
    op.drop_table('weather_samples')
    op.drop_table('weather_metars')
    op.drop_index('ix_weather_events_city_date', table_name='weather_events')
    op.drop_table('weather_events')
