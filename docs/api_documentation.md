# API

Base: `/api`. Auth: `Authorization: Bearer <token>` from `POST /api/auth/login`. All
endpoints except `health`, `time` and `auth/login` require it. Errors look like
`{"detail": ...}`. A 422 lists field locations and messages only, never the submitted values.

## Implemented (Phase 2), all covered by `backend/tests/test_phase2_api.py`

| Method | Path | Body → Response |
|---|---|---|
| GET | `/health` | → `{status, db, redis, config_problems[], server_time}` |
| GET | `/time` | → `{server_time}` (for client clock sync) |
| POST | `/auth/login` | `{username, password}` → `{token, username}` · rate limit 10 per 5 min per IP |
| GET | `/auth/me` | → `{id, username}` |
| GET | `/wallet` | → `{connected, signer_address, funder_address, signature_type, source: app\|env, last_verified_at, api_creds_ok}` |
| POST | `/wallet` | `{private_key, funder_address?, signature_type: 0\|1\|2}` → wallet status (never the key) · 20 per min |
| DELETE | `/wallet` | → `{ok, deleted}`. Hard-deletes the stored key and disables live trading |
| GET | `/strategies` | → `[{key, name, description, enabled, version, params, defaults}]` |
| GET | `/strategies/{key}` | → one strategy |
| PUT | `/strategies/{key}` | `{params}` → new version (validated against that strategy's schema) |
| POST | `/strategies/{key}/reset` | → new version with the default params |
| POST | `/strategies/{key}/enable` · `/disable` | → strategy |
| GET | `/settings` | → `{live_trading_enabled, live_allowed_by_server, bot_running, emergency_stopped, paper_bankroll, risk{…}, acceptance{…}}` |
| POST | `/settings` | `{paper_bankroll?, risk?, acceptance?}` → settings |
| GET | `/live-trading/readiness` | → `{ready, checks{…}, paper{trades, days, win_rate, profit_factor}, criteria}` |
| POST | `/live-trading/enable` | → settings, or 409 `{failed[]}` |
| POST | `/live-trading/disable` | → settings |
| POST | `/emergency-stop` · `/emergency-stop/clear` | → settings |

Strategy keys: `constellation`, `ai_strat`, `mean_reversion`, `custom_btc`, `custom_eth`,
`pocket_aces`. Each one's parameter schema is in `backend/app/strategies/catalog.py`.

## Implemented (Phase 4): markets and streams

| Method | Path | Notes |
|---|---|---|
| GET | `/markets/active` | Current window for each asset: ids, tokens, times, `up`/`down` book summary `{bid, ask, spread, ask_depth, bid_depth, last_trade, age_s, synced}`. 503 if this node isn't the market-data leader |
| GET | `/markets/history?asset=&limit=` | Recent windows with `closed`, `winner`, `ref_open_price` |
| GET | `/markets/candles/{asset}?interval=1\|5\|15\|60\|300&limit=` | OHLCV built from stored 1s bars |
| GET | `/markets/{id}` | Market row, token ids, snapshot count |
| GET | `/markets/{id}/orderbook?depth=` | Live depth for both outcomes (current window only; 409 otherwise) |
| WS | `/ws/markets` | Auth frame first. Then `{"type":"markets", ts, window{start,end,secs_in}, assets{asset:{price,bid,ask,change_24h,bar[ts,o,h,l,c],features{…},market{…}}}, feeds{…}, loop_lag_ms}` at 1 Hz, plus a `heartbeat` when idle |
| WS | `/ws/system-status` | Auth frame first. Then `{"type":"status", leader, feeds, clients}` every 5s |

`GET /health` now also returns `feeds` and is `degraded` unless the Binance and CLOB feeds are `live`.

## Planned contracts (added in the phase that makes them real)

| Phase | Endpoint | Purpose |
|---|---|---|
| 5 | `GET /strategies/{key}/signals`, `WS /ws/signals` | Signal history and stream (accepted and rejected, with reasons) |
| 5 | `POST /paper-trading/start`, `/stop` | Run the engines against the paper book |
| 6 | `GET /portfolio`, `/positions`, `/orders`, `/trades?asset=&strategy=&from=&to=&result=` | Book of record |
| 6 | `WS /ws/orders`, `/ws/positions`, `/ws/portfolio` | Lifecycle updates |
| 7 | `POST /backtests`, `GET /backtests/{id}`, `GET /analytics` | Backtests and metrics |

WebSocket auth: the client sends `{"type":"auth","token":"…"}` as its first frame. The server
sends `{"type":"heartbeat","ts"}` every 10s; clients reconnect with backoff after 25s of silence.
