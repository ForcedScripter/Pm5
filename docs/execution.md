# Execution, risk and going live (Phase 6)

## Order lifecycle

```
accepted signal (≤ 6s old) ─▶ risk engine ─▶ Order row committed (pending, unique client_key)
   ─▶ executor (paper | live) ─▶ fills ─▶ Position (one per user/mode/market/side)
   ─▶ each second: stop-loss / take-profit / DCA planner (each through the risk engine)
   ─▶ round ends: awaiting_resolution ─▶ Polymarket settles ─▶ Trade (realized P&L after fees)
```

Every step writes an `execution_logs` line linked to its signal, order and position, and is
streamed on `/ws/portfolio`. Blocks are stored in `risk_events`.

- **Idempotency.** The order row, with its unique `client_key`, is committed **before** anything is sent: `{signal}:entry`, `{position}:dca:{k}`, `{position}:stop`, `{position}:take_profit`. A crash, a retry or a second process cannot send the same order twice (tested with two managers).
- **Stale signals** older than 6s are not chased.
- **Partial fills:** entries and DCA adds are FOK, all or nothing. Exits are FAK, and an unfilled remainder rides to settlement.

## Fees (verified)

Polymarket charges takers `shares × rate × (p × (1 − p))^exponent`. These 5-minute markets use
rate 0.07, exponent 1 (`feeSchedule` on Gamma), so the fee is about 3.5% of the stake at 50¢ and
about 1% at 85¢. Our formula matches the SDK exactly, and matches a real position's
`entry_fees_usdc` from the Data API to within $0.0002. Every cost basis, P&L, signal score,
break-even rate and AI edge is **after fees**. Break-even win rate = p + 0.07·p·(1−p).

## Risk engine (`app/risk/engine.py`)

All of these apply to entries and DCA adds:
- emergency stop
- stale or unsynced book (more than 2.5s old)
- time left before resolution
- spread, slippage and minimum liquidity near the ask
- max $ per trade and per position
- max open positions and entries per market
- daily loss and daily $ bought
- drawdown from peak equity
- cooldown after N consecutive losses
- balance, including the fee

Stop-loss and take-profit sells only need a bid, a fresh book and no emergency stop. Limits are
configured in Settings → Risk limits.

## Paper vs live

| | Paper | Live |
|---|---|---|
| Fills | Walks our live copy of the Polymarket book (Phase 4), exact fees | Official `py-clob-client-v2`, marketable FOK/FAK orders |
| Cash | Ledger: bankroll + realized − open cost | Exchange collateral balance (`get_balance_allowance`) |
| When a fill counts | Immediately | Only when the exchange reports **MATCHED**; trades are then re-checked until **CONFIRMED** on-chain. A **FAILED** fill is reversed and flagged |
| Ambiguous submit | n/a | If a submit times out, the order is looked up, **never resent**. If it can't be found it's marked `submitted` for reconciliation |
| Reconciliation | n/a | Every 60s against Data API **v2** `/v2/positions` (v1 is retired). Mismatches are flagged, never overwritten |
| Winnings | Credited at settlement | Settlement P&L is booked; **you must Redeem winning shares on polymarket.com** to turn them back into USDC |

Paper and live books are separate (`mode` on every row) and never mix.

## Going live: checklist

1. **Use a dedicated wallet** that has never been pasted into any other website. Deposit only what you can afford to lose.
2. Make one small manual trade on polymarket.com with that wallet, so the exchange approvals (allowances) are set.
3. In Settings → Wallet, enter the key and your Polymarket address. It's encrypted with `MASTER_KEY` on your server. Set the signature type to *Email/Magic* (1) or *Browser wallet* (2).
4. Set `ALLOW_LIVE_TRADING=true` in `.env` and restart the backend.
5. Paper-trade until Settings → Live trading shows every check green. Defaults: ≥3 days, ≥100 paper trades, profit factor ≥1.05.
6. Click **Enable live trading**. The header badge turns to *live mode*.
7. Start small: lower the max per trade and the daily loss limit first. Watch History → Order log for the first orders.
8. **Emergency stop** (the red STOP in the header) halts the bot, disables live and cancels all open exchange orders.

**Not verified here:** no live order has been placed from this development environment, which has
no funded wallet. The live path is tested against the SDK with recorded exchange responses:
matched, delayed, unmatched, rejected, timeout and failed settlement. The first live session is
the real integration test, which is why the checklist says to start small.
