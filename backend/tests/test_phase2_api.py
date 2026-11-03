import logging

import pytest
from sqlalchemy import select

from app.core.security import decrypt_secret, redact
from app.database.session import SessionLocal
from app.models import StrategyConfiguration, Wallet
from app.services import wallet as wallet_svc
from app.strategies.catalog import CATALOG, validate_params

# throwaway key generated for tests only; never funded
TEST_KEY = "0x4c0883a69102937d6231471b5dbb6204fe5129617082792ae468d01a3f362318"
TEST_ADDR = "0x2c7536E3605D9C16a7a3D7b1898e529396a65c23"
FUNDER = "0x" + "ab" * 20


async def test_health(client):
    r = await client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["db"] and r.json()["redis"]


async def test_auth_required(client):
    for path in ("/api/strategies", "/api/wallet", "/api/settings"):
        assert (await client.get(path)).status_code == 401
    assert (await client.get("/api/strategies", headers={"Authorization": "Bearer junk"})).status_code == 401


async def test_bad_login(client):
    r = await client.post("/api/auth/login", json={"username": "admin", "password": "nope"})
    assert r.status_code == 401


async def test_strategies_seeded(client, auth):
    r = await client.get("/api/strategies", headers=auth)
    assert r.status_code == 200
    assert [s["key"] for s in r.json()] == list(CATALOG)
    const = next(s for s in r.json() if s["key"] == "constellation")
    assert const["params"]["min_consensus"] == 4 and const["params"]["entry_low"] == 0.38


async def test_strategy_update_versions_and_validates(client, auth):
    cur = (await client.get("/api/strategies/constellation", headers=auth)).json()
    p = cur["params"] | {"min_consensus": 5, "max_laggard_delta_pct": 0.1}
    r = await client.put("/api/strategies/constellation", json={"params": p}, headers=auth)
    assert r.status_code == 200 and r.json()["version"] == cur["version"] + 1
    assert r.json()["params"]["min_consensus"] == 5
    async with SessionLocal() as db:
        rows = (await db.execute(select(StrategyConfiguration).where(StrategyConfiguration.is_current))).scalars().all()
        assert len([x for x in rows if x.params.get("min_consensus") == 5]) == 1  # exactly one current row
    bad = cur["params"] | {"entry_low": 0.7, "entry_high": 0.5}
    assert (await client.put("/api/strategies/constellation", json={"params": bad}, headers=auth)).status_code == 422
    assert (await client.put("/api/strategies/nope", json={"params": {}}, headers=auth)).status_code == 404


async def test_enable_disable(client, auth):
    assert (await client.post("/api/strategies/custom_btc/enable", headers=auth)).json()["enabled"] is True
    assert (await client.post("/api/strategies/custom_btc/disable", headers=auth)).json()["enabled"] is False


def test_custom_asset_is_pinned():
    assert validate_params("custom_eth", {"assets": ["btc", "sol"]})["assets"] == ["eth"]


def test_dca_stage_sum_must_fit_ceiling():
    with pytest.raises(Exception):
        validate_params("custom_btc", {"dca": {"stages": [5, 5, 5], "max_total": 10}})


async def test_wallet_is_encrypted_and_never_returned(client, auth, monkeypatch, caplog):
    fake_creds = {"api_key": "k", "api_secret": "s", "api_passphrase": "p"}
    monkeypatch.setattr(wallet_svc, "derive_api_creds", lambda *a: fake_creds)
    # save_wallet's default arg was bound at import; patch the route's call path
    orig = wallet_svc.save_wallet
    monkeypatch.setattr(wallet_svc, "save_wallet",
                        lambda db, uid, k, f, t: orig(db, uid, k, f, t, verify=lambda *a: fake_creds))
    caplog.set_level(logging.DEBUG)

    r = await client.post("/api/wallet", headers=auth,
                          json={"private_key": TEST_KEY, "funder_address": FUNDER, "signature_type": 1})
    assert r.status_code == 200, r.text
    body = r.text
    assert TEST_KEY[2:] not in body and r.json()["signer_address"] == TEST_ADDR
    assert TEST_KEY[2:] not in caplog.text

    async with SessionLocal() as db:
        w = (await db.execute(select(Wallet))).scalar_one()
        assert TEST_KEY.encode() not in w.encrypted_key           # ciphertext, not plaintext
        assert decrypt_secret(w.encrypted_key) == TEST_KEY

    st = (await client.get("/api/wallet", headers=auth)).json()
    assert st["connected"] and st["source"] == "app" and "private_key" not in st

    assert (await client.delete("/api/wallet", headers=auth)).json()["deleted"] == 1
    async with SessionLocal() as db:
        assert (await db.execute(select(Wallet))).first() is None   # hard-deleted
    assert (await client.get("/api/wallet", headers=auth)).json()["connected"] is False


async def test_wallet_rejects_bad_input(client, auth):
    r = await client.post("/api/wallet", headers=auth, json={"private_key": "zz" * 32, "signature_type": 0})
    assert r.status_code == 422 and "zz" * 32 not in r.text
    r = await client.post("/api/wallet", headers=auth, json={"private_key": TEST_KEY, "signature_type": 1})
    assert r.status_code == 400  # proxy sig type needs a funder address


def test_redaction():
    assert "4c0883a6" not in redact(f"oops key={TEST_KEY}")


async def test_live_gate_requires_paper_record(client, auth):
    r = await client.get("/api/live-trading/readiness", headers=auth)
    assert r.json()["ready"] is False and r.json()["checks"]["paper_trades"] is False
    r = await client.post("/api/live-trading/enable", headers=auth)
    assert r.status_code == 409 and "paper_trades" in r.json()["detail"]["failed"]


async def test_emergency_stop(client, auth):
    r = await client.post("/api/emergency-stop", headers=auth)
    s = r.json()
    assert s["emergency_stopped"] and not s["live_trading_enabled"] and not s["bot_running"]
    ready = (await client.get("/api/live-trading/readiness", headers=auth)).json()
    assert ready["checks"]["not_emergency_stopped"] is False
    assert (await client.post("/api/emergency-stop/clear", headers=auth)).json()["emergency_stopped"] is False


async def test_settings_roundtrip(client, auth):
    cur = (await client.get("/api/settings", headers=auth)).json()
    risk = cur["risk"] | {"max_daily_loss_usd": 25}
    r = await client.post("/api/settings", headers=auth, json={"risk": risk, "paper_bankroll": 300})
    assert r.json()["risk"]["max_daily_loss_usd"] == 25 and r.json()["paper_bankroll"] == 300
    bad = cur["risk"] | {"max_spread": 2}
    assert (await client.post("/api/settings", headers=auth, json={"risk": bad})).status_code == 422
