# pm5bot

A self-hosted automated trading platform for Polymarket's 5-minute crypto Up/Down markets
(BTC, ETH, SOL, XRP, DOGE, BNB). Its features are reconstructed from a public reference
console; see `docs/reverse_engineering.md`.

> ⚠️ Prediction-market trading can lose money. The reference service advertised ~80% win
> rates, but its own public signal data shows ~54%, which is about break-even. Nothing here
> is proven profitable until our own paper and out-of-sample results say so.

## Status

| Phase | State |
|---|---|
| 1. Reference investigation | ✅ `docs/reverse_engineering.md` |
| 2. Architecture, DB, API core, Docker | ✅ `docs/architecture.md`, `docs/api_documentation.md` |
| 3. Frontend | next |
| 4–8. Market data → strategies → execution → backtesting → hardening | planned |

`legacy_v0/` holds the first single-file bot. It already paper-trades end to end, and it's
the reference for porting strategy logic in Phase 5.

## Run it

```bash
cp .env.example .env      # then fill in MASTER_KEY, JWT_SECRET, ADMIN_PASSWORD, POSTGRES_PASSWORD
docker compose up -d --build
curl localhost:8000/api/health
```

## Develop and test

```bash
python3 -m venv .venv && .venv/bin/pip install -r backend/requirements-dev.txt
# needs a local Postgres with database pm5_test, plus Redis on 6379
cd backend && ../.venv/bin/python -m pytest
```

## Wallet

Connect it in the website (Settings → Wallet). The key goes only to your own backend, which
encrypts it with `MASTER_KEY` and never returns or logs it. Use a dedicated trading wallet.
Details are in `docs/architecture.md`.
