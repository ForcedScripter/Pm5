"""First-boot seeding: strategy catalog, admin user, per-user settings and configs."""
from __future__ import annotations

import logging

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import hash_password
from app.models import Strategy, StrategyConfiguration, User, UserSettings
from app.schemas import AcceptanceCriteria, RiskSettings
from app.strategies.catalog import CATALOG, defaults, validate_params

log = logging.getLogger(__name__)


async def seed(db: AsyncSession) -> None:
    existing = {s.key: s for s in (await db.execute(select(Strategy))).scalars()}
    for key, (name, desc, _) in CATALOG.items():
        if key in existing:
            existing[key].name, existing[key].description, existing[key].default_params = name, desc, defaults(key)
        else:
            db.add(Strategy(key=key, name=name, description=desc, default_params=defaults(key)))
    # Retired strategies (no longer in CATALOG) keep their rows: positions, signals and orders reference
    # their configs by FK. We only switch them off so nothing trades or lists them.
    retired = [strat.id for key, strat in existing.items() if key not in CATALOG]
    if retired:
        await db.execute(update(StrategyConfiguration).where(StrategyConfiguration.strategy_id.in_(retired),
                                                             StrategyConfiguration.enabled).values(enabled=False))
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
    await db.flush()
    await upgrade_params(db)
    await db.commit()


def _upgraded(key: str, p: dict) -> dict | None:
    """Default changes that should reach existing configs, applied only where the stored value is
    still the OLD default (a value the user chose is never overwritten). None = nothing to change."""
    q = {**p, "exits": dict(p.get("exits") or {})}
    if key == "spork_maker" and "queue_ahead" not in p:
        return defaults(key) | {"assets": p.get("assets") or defaults(key)["assets"]}   # pre-rebuild schema
    if key == "binance_lead" and p.get("lead_threshold_bps") == 8.0:
        q["lead_threshold_bps"] = 12.0                    # Binance/Chainlink disagree under ~12 bps (npcnpc09)
    if key in ("pocket_aces", "binance_lead") and q["exits"].get("strike_crossing_stop") is True:
        q["exits"]["strike_crossing_stop"] = False        # was default-on; its evidence was refuted
    return q if q != {**p, "exits": dict(p.get("exits") or {})} else None


async def upgrade_params(db: AsyncSession) -> None:
    """Write a new config VERSION (configs are versioned so past signals keep their exact params)."""
    rows = (await db.execute(select(Strategy.key, StrategyConfiguration).join(
        StrategyConfiguration, StrategyConfiguration.strategy_id == Strategy.id)
        .where(StrategyConfiguration.is_current, Strategy.key.in_(list(CATALOG))))).all()
    for key, cur in rows:
        new = _upgraded(key, cur.params or {})
        if new is None:
            continue
        try:
            new = validate_params(key, new)
        except Exception:  # noqa: BLE001 - leave an unparseable config to the user
            log.warning("could not upgrade %s config %s", key, cur.id)
            continue
        cur.is_current = False
        db.add(StrategyConfiguration(user_id=cur.user_id, strategy_id=cur.strategy_id, version=cur.version + 1,
                                     is_current=True, enabled=cur.enabled, params=new))
        log.info("upgraded %s config to v%d (new defaults)", key, cur.version + 1)
    await db.flush()


async def ensure_user_rows(db: AsyncSession, user: User) -> None:
    if await db.get(UserSettings, user.id) is None:
        db.add(UserSettings(user_id=user.id, risk=RiskSettings().model_dump(),
                            acceptance=AcceptanceCriteria().model_dump()))
    have = set((await db.execute(select(StrategyConfiguration.strategy_id).where(
        StrategyConfiguration.user_id == user.id, StrategyConfiguration.is_current))).scalars())
    for strat in (await db.execute(select(Strategy).where(Strategy.key.in_(list(CATALOG))))).scalars():
        if strat.id not in have:
            db.add(StrategyConfiguration(user_id=user.id, strategy_id=strat.id, version=1,
                                         is_current=True, enabled=False, params=defaults(strat.key)))
