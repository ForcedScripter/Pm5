# Architecture (Phase 2)

## Services

```
                 ┌──────────────────────── your server (self-hosted) ───────────────────────┐
 Browser ──TLS──▶│ frontend (React, Phase 3) ──▶ backend (FastAPI)  ──▶ PostgreSQL (truth)   │
                 │                                 │   ▲                 Redis (live state)  │
                 │                                 ▼   │                                    │
                 │   market-data ◀── Binance WS    strategy engines ─▶ risk ─▶ execution ───┼──▶ Polymarket CLOB
                 │   discovery   ◀── Gamma API     (Phase 5)          (P6)    (P6)          │◀── Data API (reconcile)
                 └──────────────────────────────────────────────────────────────────────────┘
```

| Service | Status | Notes |
|---|---|---|
| `postgres` | ✅ | Source of truth for configs, signals, orders, fills, positions and trades |
| `redis` | ✅ | Live snapshots, event streams, locks (Phase 4+) |
| `backend` | ✅ API core | Auth, wallet, strategy configs, settings, live gate, emergency stop, health |
| market-data / discovery | Phase 4 | Runs inside the backend process as asyncio tasks; can be split into its own container later |
| strategy engines | Phase 5 | constellation, ai_strat, mean_reversion, custom_btc/eth, pocket_aces |
| risk + execution | Phase 6 | One shared risk engine; paper and live executors |
| frontend | Phase 3 | React 19, Vite, Tailwind, shadcn/ui |

## Event pipeline (Phases 4–6)

```
Binance WS ─▶ collector ─▶ XADD md:{asset}        (Redis Stream, 1s bars + ticks)
Gamma/CLOB ─▶ registry  ─▶ HSET market:{slug}     (tokens, window, book top)
                              │
                 feature engine (returns 1/5/10/15/30/60s, vol, z, consensus, laggard score)
                              ▼
                 strategy engines ─▶ signals (PG) ─▶ risk engine ─▶ orders (PG, idempotent client_key)
                              ▼                                         ▼
                 XADD ev:signals / ev:orders / ev:positions ─▶ WebSocket fan-out ─▶ dashboard
```

## Data model

18 tables (`backend/app/models/__init__.py`, migration `ca1b651299b9`):

- users
- wallets
- user_settings
- strategies
- strategy_configurations (versioned)
- markets
- market_tokens
- market_snapshots
- price_history
- signals
- orders
- order_fills
- positions
- trades
- portfolio_snapshots
- execution_logs
- risk_events
- model_predictions

Key invariants:
- Every trading row has `mode` = `paper` | `live`, and all queries filter on it, so the paper and live books never mix.
- `orders.client_key` is unique and deterministic for each (position, purpose, step). That makes retries idempotent: a retried DCA step can't double-buy.
- `positions` has a unique (user, mode, market, side), so DCA adds to an existing position instead of duplicating it.
- `strategy_configurations` are append-only versions. Each signal points at the exact parameter version it used.
- A fill exists only once the exchange confirms it (`order_fills`). Positions are derived from fills, never from intent.

## Wallet and key security model (as chosen by the owner)

The wallet is connected **in the website** (Settings → Wallet). The key goes only to **your
own backend**, never to a third party:

1. The browser POSTs `{private_key, funder_address, signature_type}` to `POST /api/wallet`. That endpoint requires a logged-in session and is rate-limited. Use it only over HTTPS or localhost.
2. The backend checks the key format, derives the signer address, and asks Polymarket for L2 API credentials (signed locally; the key isn't sent).
3. The key and the credentials are encrypted with Fernet using `MASTER_KEY` from the server environment, then stored in `wallets`.
4. No endpoint ever returns the key. Validation errors never echo inputs (custom 422 handler). Logs pass through a redactor that strips 64-hex strings. `WalletIn` and `SignerMaterial` override `__repr__`.
5. "Disconnect" hard-deletes the ciphertext and switches live trading off.
6. Fallback: `PRIVATE_KEY` in `.env`, used only if no wallet is saved in the app.

What this protects against: a stolen database alone, leaked logs, and UI or API responses.
What it doesn't: someone with root on your server, who could read `MASTER_KEY` and the
database. Use a dedicated trading wallet holding only what you're willing to trade.

## Live-trading gate (three locks)

1. `ALLOW_LIVE_TRADING=true` must be set in the server environment.
2. The in-app switch (`POST /api/live-trading/enable`) only turns on when a wallet is connected, emergency stop is clear, and the **paper acceptance criteria** pass. Defaults: ≥3 days, ≥100 trades, profit factor ≥1.05. All are configurable.
3. `POST /api/emergency-stop` turns everything off immediately. From Phase 6 it also cancels all open exchange orders.

## Deployment

`docker compose up -d --build` starts postgres, redis and the backend. The backend runs
`alembic upgrade head` on start and binds to `127.0.0.1:8000` only. Put Caddy or nginx with
TLS in front, or use an SSH tunnel. Images come from `public.ecr.aws/docker/library` (the
same official images, without Docker Hub's anonymous pull limits).
