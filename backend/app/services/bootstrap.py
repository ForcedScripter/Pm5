"""First-boot seeding: strategy catalog, admin user, per-user settings and configs."""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import hash_password
from app.models import Strategy, StrategyConfiguration, User, UserSettings
from app.schemas import AcceptanceCriteria, RiskSettings
from app.strategies.catalog import CATALOG, defaults

log = logging.getLogger(__name__)


async def seed(db: AsyncSession) -> None:
    existing = {s.key: s for s in (await db.execute(select(Strategy))).scalars()}
    for key, (name, desc, _) in CATALOG.items():
        if key in existing:
            existing[key].name, existing[key].description, existing[key].default_params = name, desc, defaults(key)
        else:
            db.add(Strategy(key=key, name=name, description=desc, default_params=defaults(key)))
    await db.flush()

    s = get_settings()
    admin = (await db.execute(select(User).where(User.username == "admin"))).scalar_one_or_none()
    if admin is None and s.admin_password:
        admin = User(username="admin", password_hash=hash_password(s.admin_password))
        db.add(admin)
        await db.flush()
        log.info("created admin user")
    for user in (await db.execute(select(User))).scalars():
        await ensure_user_rows(db, user)
    await db.commit()


async def ensure_user_rows(db: AsyncSession, user: User) -> None:
    if await db.get(UserSettings, user.id) is None:
        db.add(UserSettings(user_id=user.id, risk=RiskSettings().model_dump(),
                            acceptance=AcceptanceCriteria().model_dump()))
    have = set((await db.execute(select(StrategyConfiguration.strategy_id).where(
        StrategyConfiguration.user_id == user.id, StrategyConfiguration.is_current))).scalars())
    for strat in (await db.execute(select(Strategy))).scalars():
        if strat.id not in have:
            db.add(StrategyConfiguration(user_id=user.id, strategy_id=strat.id, version=1,
                                         is_current=True, enabled=False, params=defaults(strat.key)))
