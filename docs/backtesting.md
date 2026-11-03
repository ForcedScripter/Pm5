# Backtesting (Phase 7)

The backtester replays stored history through **the same code the live bot runs**:
- the strategy functions (`strategies/engine/rules.py`, `strategies/ai/strategy.py`)
- the risk engine
- the DCA planner
- the stop-loss and take-profit rules
- Polymarket's taker-fee formula

The only difference is where prices come from.

## Data: two fidelity tiers

| Tier | Where it comes from | Book data | How it's simulated |
|---|---|---|---|
| **Recorded** | Collected by this server while running (Phase 4) | Real bid/ask/spread/depth every 5s | Evaluated every snapshot. A fill needs a book within 5s after the latency, at ≤ signal ask + slippage, with stake ≤ resting depth (FOK) |
| **Backfilled** | Public history: Binance 1s klines, Gamma markets + winners, CLOB `/prices-history` | **One price per minute**, no spread or depth | Ask/bid = price ± half the median spread seen live (≥ 0.5¢). Fill at the **signal-time** price + **1¢ adverse slippage**. Depth isn't enforced |

Settings → Backtest → **Backfill history** downloads up to 14 days. It's idempotent and resumes
after interruptions. Every result is labelled with its mix of tiers, and backfilled results carry
a warning.

## No look-ahead

- Features at time *t* use only 1s bars ≤ *t* (`ArraySource`, which is parity-tested against the live `BarBuffer`).
- The book at *t* is the latest snapshot ≤ *t*.
- A decision at *t* executes at the first book ≥ *t* + latency (default 2s), never at *t*. If the price ran past the limit, it's a **miss**, not a fill.
- Streak reversal only sees rounds that had **settled** by *t* (round end + 5 minutes).
- **AI Strat is walk-forward:** it's trained only on rounds *before* the out-of-sample split (plus up to 7 days of earlier history). Its in-sample numbers overlap its training data, which the results say.

## Validation

- **Chronological split:** the last 30% of the period is out-of-sample, reported side by side with in-sample. If a strategy only makes money in-sample, the report says so.
- **Uncertainty:** win rates come with a 95% Wilson interval. Fewer than 100 trades triggers a warning.
- **Fees:** the break-even win rate shown is fee-adjusted: p + 0.07·p·(1−p).

## Risk modes

- **My risk limits:** your real Settings → Risk limits. This is what the bot would actually have done. Portfolio caps such as daily exposure can block most entries late in a period.
- **Strategy edge:** lifts only the portfolio caps (daily exposure and loss, open positions, drawdown, cooldown). Per-trade size, book gates, latency and fees still apply. Use this to judge each strategy's signals on their own.

## Metrics

All computed after fees:
- trades and trades per day
- win rate (95% CI) vs fee-adjusted break-even
- net P&L, total return, ROI on $ staked
- profit factor, EV per trade, average win and loss
- max drawdown ($ and %)
- Sharpe: daily and annualized when the period has 5 or more days, otherwise per trade
- fees paid
- breakdowns by strategy, coin and fidelity tier
- a pipeline section: signals, entries, DCA adds, exits, misses (latency, price moved, depth), risk blocks by rule, and top skip reasons

The same calculator powers `GET /api/analytics` on your real paper and live trades.

## Isolation

Backfills and backtests run in a separate worker **process**, with their own event loop and
database connections, and report progress through Redis. A thread wasn't enough: CPU work in
a thread still stalled the live loop by 250–350ms and caused a Polymarket slow-consumer
disconnect. Only one batch job runs at a time.

## Known limitations

- Polymarket publishes no historical order books, so backfilled rounds can't model depth or the true spread. Only recorded rounds can. Leave the backend running to accumulate recorded history.
- Paper and backtest fills don't consume liquidity from the book; your own orders' market impact isn't modelled.
- The resolution oracle (Chainlink 60s TWAP) isn't replayed. Winners come from Polymarket's settled outcome, which is the ground truth, but our signal reference is Binance's open, as it is live.
