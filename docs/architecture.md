# Architecture (updated Phase 4)

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
| `redis` | ✅ | Market-data leader lock, live spot/book snapshots, 1s bar streams |
| `backend` | ✅ API core | Auth, wallet, strategy configs, settings, live gate, emergency stop, health |
| market-data / discovery | ✅ Phase 4 | asyncio tasks inside the backend, see "Market data pipeline" below |
| strategy engines | Phase 5 | constellation, ai_strat, mean_reversion, custom_btc/eth, pocket_aces |
| risk + execution | Phase 6 | One shared risk engine; paper and live executors |
| frontend | ✅ Phase 3 | React 19, Vite, Tailwind, shadcn/ui; since Phase 4 it talks only to the backend |

## Market data pipeline (Phase 4)

```
Binance WS (kline_1s, bookTicker, miniTicker × 6) ─▶ BinanceCollector ─▶ BarBuffer (memory, 65 min)
     ▲ REST klines backfill on boot + after every reconnect gap         ├─▶ price_history (PG, batched 500/chunk)
                                                                         └─▶ Redis: pm5:md:{asset} stream, pm5:spot:{asset}
Gamma /events?slug={asset}-updown-5m-{start} ─▶ Registry (every 2s)
     current + next window for 6 assets → markets / market_tokens (PG); records ref open price;
     polls ended windows until Polymarket settles → markets.closed / winner / resolved_at
CLOB WS market channel, 3 connections × 2 assets ─▶ BookService: snapshot + price_change deltas → OrderBook
     resubscribes on rollover; reloads a book from REST if it ever crosses;
     every 5s → market_snapshots (PG) + Redis pm5:book:{token}
Hub ─▶ 1 Hz `markets` frame (spot, features, book summaries, feed health) ─▶ /ws/markets ─▶ browser
```

**Features** (`market_data/bars.py`, pure): returns over 1/5/10/15/30/60s, the round move since
window open, 60s realised volatility, and 1h return. They're computed only from bars at or before
`now`, so the same code replays in backtests with no look-ahead.

**Reliability** (each item has a test in `tests/test_phase4_failures.py` or `tests/live/`):
- Reconnects use exponential backoff (1s up to 30s). After a Binance reconnect, the gap is backfilled from REST.
- A feed silent for 15s (Binance) or 30s (CLOB) is treated as dead and reconnected. Health reports `live` / `stale` / `down` per feed.
- Polymarket disconnects slow readers with **1013 "slow consumer"**. Books are sharded over 3 connections of 2 assets each so no single socket carries all the burst traffic. So the WS clients use large receive queues, the book hot path is about 5µs per message, crossed-book checks are batched once per second, and blocking work runs in threads: bcrypt logins and the SDK's synchronous HTTP. The bar writer yields every 500 rows. A loop-lag monitor logs any stall over 250ms.
- If the database is down, bars stay queued and nothing is lost; memory is capped at 50k bars.
- Leader election: `SET pm5:md:leader NX EX 15`, renewed every 5s. Standby nodes keep retrying, so a restart or a crashed leader is taken over automatically.
- Validated live: across 28 consecutive server snapshots, the delta-maintained books matched exactly (`tests/live`, `RUN_LIVE=1`).
- Retention: 1s bars are kept 30 days and book snapshots 90 days (configurable).

**Observed in a 10-minute soak test** (sandbox network): occasional drops on both Binance and
Polymarket. Most were abrupt TCP cuts from the sandbox proxy; two were 1013s on the busiest
shard. Each one recovered in about a second, with fresh book snapshots and REST-backfilled bars,
and no data was lost. **Consequence for Phase 6:** execution must refuse to trade on a book whose
`synced` is false or whose `age_s` is more than about 2s.

**Known limitation:** Polymarket settles on Chainlink's 60s TWAP, while our signal reference
is Binance's 1s open. They usually agree, but they can disagree on very small moves.

## Event pipeline (Phases 5–6)

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

## Deployment (your own PC)

`scripts/setup.(sh|ps1)` writes `.env` with random secrets and runs `docker compose up -d --build`.
Ports bind to `127.0.0.1` only. Containers restart automatically (`unless-stopped`), logs
rotate (3 × 10 MB per container), and the backend runs DB migrations on every start.
`scripts/update.*` does `git pull` and rebuilds; `scripts/backup.*` dumps Postgres to `./backups/`.
Batch jobs (backfill, backtests, AI training) run in a separate worker process inside the
backend container, so they never stall the live feeds.

## Deployment (server, optional)

`docker compose up -d --build` starts postgres, redis and the backend. The backend runs
`alembic upgrade head` on start and binds to `127.0.0.1:8000` only. Put Caddy or nginx with
TLS in front, or use an SSH tunnel. Images come from `public.ecr.aws/docker/library` (the
same official images, without Docker Hub's anonymous pull limits).
