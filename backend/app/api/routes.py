from __future__ import annotations

import asyncio
import datetime as dt
import time

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user, login_limit, sensitive_limit
from app.core.config import get_settings
from app.core.security import make_token, verify_password
from app.database.session import get_db
from app.models import ExecutionLog, Strategy, StrategyConfiguration, Trade, User, UserSettings
from app.schemas import (AcceptanceCriteria, LoginIn, RiskSettings, SettingsIn, SettingsOut, StrategyOut,
                         StrategyParamsIn, TokenOut, WalletIn, WalletOut)
from app.services import wallet as wallet_svc
from app.strategies.catalog import CATALOG, defaults, validate_params

router = APIRouter(prefix="/api")


# ── auth ─────────────────────────────────────────────────────────────────
@router.post("/auth/login", response_model=TokenOut, dependencies=[Depends(login_limit)])
async def login(body: LoginIn, db: AsyncSession = Depends(get_db)):
    user = (await db.execute(select(User).where(User.username == body.username))).scalar_one_or_none()
    # bcrypt is deliberately slow (~250 ms CPU): run it off the event loop so logins don't stall the feeds
    ok = bool(user) and user.is_active and await asyncio.to_thread(verify_password, body.password, user.password_hash)
    if not ok:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "wrong username or password")
    return TokenOut(token=make_token(str(user.id)), username=user.username)


@router.get("/auth/me")
async def me(user: User = Depends(current_user)):
    return {"id": str(user.id), "username": user.username}


# ── wallet ───────────────────────────────────────────────────────────────
def _wallet_out(w, source="app") -> WalletOut:
    return WalletOut(connected=True, signer_address=w.signer_address, funder_address=w.funder_address,
                     signature_type=w.signature_type, source=source, last_verified_at=w.last_verified_at,
                     api_creds_ok=w.encrypted_api_creds is not None)


@router.get("/wallet", response_model=WalletOut)
async def wallet_status(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    w = await wallet_svc.active_wallet(db, user.id)
    if w:
        return _wallet_out(w)
    signer = await wallet_svc.load_signer(db, user.id)
    if signer:
        return WalletOut(connected=True, signer_address=signer.signer_address, funder_address=signer.funder_address,
                         signature_type=signer.signature_type, source="env")
    return WalletOut(connected=False)


@router.post("/wallet", response_model=WalletOut, dependencies=[Depends(sensitive_limit)])
async def wallet_connect(body: WalletIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    try:
        w = await wallet_svc.save_wallet(db, user.id, body.private_key, body.funder_address, body.signature_type)
    except wallet_svc.WalletError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from None
    return _wallet_out(w)


@router.delete("/wallet")
async def wallet_disconnect(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    n = await wallet_svc.remove_wallet(db, user.id)
    await db.execute(update(UserSettings).where(UserSettings.user_id == user.id)
                     .values(live_trading_enabled=False))
    await db.commit()
    return {"ok": True, "deleted": n}


# ── strategies ───────────────────────────────────────────────────────────
async def _current_cfg(db, user_id, key) -> tuple[Strategy, StrategyConfiguration]:
    if key not in CATALOG:
        raise HTTPException(404, f"unknown strategy '{key}'")
    row = (await db.execute(
        select(Strategy, StrategyConfiguration).join(StrategyConfiguration, StrategyConfiguration.strategy_id == Strategy.id)
        .where(Strategy.key == key, StrategyConfiguration.user_id == user_id, StrategyConfiguration.is_current)
    )).first()
    if not row:
        raise HTTPException(404, "strategy not initialised")
    return row[0], row[1]


def _strat_out(s: Strategy, c: StrategyConfiguration) -> StrategyOut:
    return StrategyOut(key=s.key, name=s.name, description=s.description, enabled=c.enabled,
                       version=c.version, params=c.params, defaults=defaults(s.key))


@router.get("/strategies", response_model=list[StrategyOut])
async def list_strategies(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(
        select(Strategy, StrategyConfiguration).join(StrategyConfiguration, StrategyConfiguration.strategy_id == Strategy.id)
        .where(StrategyConfiguration.user_id == user.id, StrategyConfiguration.is_current)
    )).all()
    order = list(CATALOG)
    return sorted((_strat_out(s, c) for s, c in rows), key=lambda x: order.index(x.key))


@router.get("/strategies/{key}", response_model=StrategyOut)
async def get_strategy(key: str, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    return _strat_out(*await _current_cfg(db, user.id, key))


@router.put("/strategies/{key}", response_model=StrategyOut)
async def update_strategy(key: str, body: StrategyParamsIn, user: User = Depends(current_user),
                          db: AsyncSession = Depends(get_db)):
    s, cur = await _current_cfg(db, user.id, key)
    try:
        params = validate_params(key, body.params)
    except ValidationError as e:
        raise HTTPException(422, e.errors(include_url=False, include_context=False)) from None
    cur.is_current = False
    new = StrategyConfiguration(user_id=user.id, strategy_id=s.id, version=cur.version + 1,
                                is_current=True, enabled=cur.enabled, params=params)
    db.add(new)
    await db.commit()
    return _strat_out(s, new)


@router.post("/strategies/{key}/reset", response_model=StrategyOut)
async def reset_strategy(key: str, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    return await update_strategy(key, StrategyParamsIn(params=defaults(key)), user, db)


async def _set_enabled(key, flag, user, db):
    s, c = await _current_cfg(db, user.id, key)
    c.enabled = flag
    await db.commit()
    return _strat_out(s, c)


@router.post("/strategies/{key}/enable", response_model=StrategyOut)
async def enable_strategy(key: str, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    return await _set_enabled(key, True, user, db)


@router.post("/strategies/{key}/disable", response_model=StrategyOut)
async def disable_strategy(key: str, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    return await _set_enabled(key, False, user, db)


# ── settings / live gate / emergency stop ────────────────────────────────
def _settings_out(us: UserSettings) -> SettingsOut:
    return SettingsOut(live_trading_enabled=us.live_trading_enabled,
                       live_allowed_by_server=get_settings().allow_live_trading,
                       bot_running=us.bot_running, emergency_stopped=us.emergency_stopped,
                       paper_bankroll=float(us.paper_bankroll),
                       risk=RiskSettings(**(us.risk or {})), acceptance=AcceptanceCriteria(**(us.acceptance or {})))


async def _us(db, user) -> UserSettings:
    us = await db.get(UserSettings, user.id)
    if not us:
        raise HTTPException(500, "user settings missing")
    return us


@router.get("/settings", response_model=SettingsOut)
async def get_settings_ep(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    return _settings_out(await _us(db, user))


@router.post("/settings", response_model=SettingsOut)
async def save_settings(body: SettingsIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    us = await _us(db, user)
    if body.paper_bankroll is not None:
        us.paper_bankroll = body.paper_bankroll
    if body.risk is not None:
        us.risk = body.risk.model_dump()
    if body.acceptance is not None:
        us.acceptance = body.acceptance.model_dump()
    await db.commit()
    return _settings_out(us)


async def paper_track_record(db: AsyncSession, user_id) -> dict:
    q = select(func.count(), func.min(Trade.closed_at),
               func.coalesce(func.sum(Trade.pnl).filter(Trade.pnl > 0), 0),
               func.coalesce(-func.sum(Trade.pnl).filter(Trade.pnl < 0), 0),
               func.count().filter(Trade.pnl > 0),
               func.coalesce(func.sum(Trade.pnl), 0)).where(Trade.user_id == user_id, Trade.mode == "paper")
    n, first, gross_win, gross_loss, wins, pnl = (await db.execute(q)).one()
    days = (dt.datetime.now(dt.UTC) - first).total_seconds() / 86400 if first else 0
    return {"trades": n, "wins": wins, "pnl": float(pnl), "days": round(days, 2), "win_rate": (wins / n) if n else 0,
            # JSON has no Infinity: a record with wins and no losses reports a large sentinel
            "profit_factor": float(gross_win / gross_loss) if gross_loss else (999.0 if gross_win else 0.0)}


@router.get("/live-trading/readiness")
async def live_readiness(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    us = await _us(db, user)
    acc = AcceptanceCriteria(**(us.acceptance or {}))
    rec = await paper_track_record(db, user.id)
    signer = await wallet_svc.active_wallet(db, user.id) or get_settings().private_key
    checks = {
        "server_allows_live": get_settings().allow_live_trading,
        "wallet_connected": bool(signer),
        "not_emergency_stopped": not us.emergency_stopped,
        "paper_days": rec["days"] >= acc.min_paper_days,
        "paper_trades": rec["trades"] >= acc.min_paper_trades,
        "paper_win_rate": rec["win_rate"] >= acc.min_win_rate,
        "paper_profit_factor": rec["profit_factor"] >= acc.min_profit_factor,
    }
    return {"ready": all(checks.values()), "checks": checks, "paper": rec, "criteria": acc.model_dump()}


@router.post("/live-trading/enable", response_model=SettingsOut, dependencies=[Depends(sensitive_limit)])
async def live_enable(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    r = await live_readiness(user, db)
    if not r["ready"]:
        failed = [k for k, v in r["checks"].items() if not v]
        raise HTTPException(409, {"message": "live trading is not allowed yet", "failed": failed, "paper": r["paper"]})
    us = await _us(db, user)
    us.live_trading_enabled = True
    await db.commit()
    return _settings_out(us)


@router.post("/live-trading/disable", response_model=SettingsOut)
async def live_disable(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    us = await _us(db, user)
    us.live_trading_enabled = False
    await db.commit()
    return _settings_out(us)


@router.post("/emergency-stop", response_model=SettingsOut)
async def emergency_stop(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """Halts all trading immediately: bot off, live off, no new orders until cleared.
    (From Phase 6 this also cancels every open exchange order.)"""
    us = await _us(db, user)
    us.emergency_stopped, us.bot_running, us.live_trading_enabled = True, False, False
    await db.commit()
    return _settings_out(us)


@router.post("/emergency-stop/clear", response_model=SettingsOut)
async def emergency_clear(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    us = await _us(db, user)
    us.emergency_stopped = False
    await db.commit()
    return _settings_out(us)


LOG_CATEGORIES = {
    "all": None,
    "trades": ("order", "fill", "position", "settle"),
    "skips": ("skip", "risk"),
    "signals": ("signal",),
    "errors": None,
    "system": ("system",),
}


@router.get("/logs")
async def logs(category: str = "all", before_id: int | None = None, limit: int = 100,
               user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    if category not in LOG_CATEGORIES:
        raise HTTPException(400, f"category must be one of {sorted(LOG_CATEGORIES)}")
    q = select(ExecutionLog).where((ExecutionLog.user_id == user.id) | (ExecutionLog.user_id.is_(None)))
    if category == "errors":
        q = q.where(ExecutionLog.level == "error")
    elif LOG_CATEGORIES[category]:
        q = q.where(ExecutionLog.category.in_(LOG_CATEGORIES[category]))
    if before_id:
        q = q.where(ExecutionLog.id < before_id)
    rows = (await db.execute(q.order_by(ExecutionLog.id.desc()).limit(max(1, min(limit, 500))))).scalars()
    return [{"id": r.id, "ts": r.ts.isoformat(), "level": r.level, "category": r.category,
             "message": r.message, "data": r.data} for r in rows]


@router.get("/time")
async def server_time():
    return {"server_time": time.time()}
