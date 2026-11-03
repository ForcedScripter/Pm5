# Strategies (Phase 5)

Every strategy is a pure function over the same market snapshot: 1-second spot features from
Binance, plus the current round's Polymarket books. It returns **decisions**. A decision exists
only once the strategy's *trigger* fires. It is **taken** if every gate passes, and otherwise
**skipped** with the first failing gate as its reason.

The runner evaluates every enabled strategy once a second while your bot is running. It records:
- every taken call
- one *near miss* per (strategy, coin, side, reason) per round

Every record is scored when Polymarket settles the round. Source: `backend/app/strategies/`.

> Parameters marked **[ref]** were observed in the reference app (`docs/reverse_engineering.md`).
> Anything labelled *heuristic* is our own; the reference's formula wasn't observable.

## Shared gates (applied in this order)

| Gate | Skips when | Params |
|---|---|---|
| direction | the side is switched off, or bias excludes it | `trade_up`, `trade_down`, `bias` |
| timing | the current UTC hour is skipped, or we're outside the entry window | `window.skip_hours_utc`, `first_check_s` (45 [ref]), `last_entry_s` (210 [ref]) |
| strategy-specific | e.g. the consensus average is too small, the edge is too small, cooldown | see below |
| book | no book; book not synced or older than 2.5s; spread too wide; too little $ near the ask | `max_spread`, `min_depth_usd` |
| price | ask outside the buy band, or inside the avoid band | `entry_low`/`entry_high` [ref], `avoid_low`/`avoid_high` [ref] |

Out-of-window and no-book rejections aren't recorded as near misses, because they aren't decisions.

## Constellation (reference default)

1. A coin "moves" when its move since the round opened passes ±`min_spot_delta_pct` (0.01% [ref]).
2. **Trigger:** at least `min_consensus` (4 [ref]) *other* coins move the same way, and one of your selected coins hasn't moved with them. A laggard has moved less than the threshold in that direction, and by at most `max_laggard_delta_pct` (0.15% [ref]) either way.
3. **Consensus gate:** the group's average move must clear `up_threshold` (0.055% [ref]) or `down_threshold` (0.10% [ref]).
4. **BTC regime filter [ref]:** a BTC 1h change of +2% or more lowers the DOWN threshold to 0.06%; −2% or less lowers the UP threshold to 0.03%.
5. **Streak reversal [ref, off by default]:** after N identical outcomes in a row, take the other side.
6. **Confidence (heuristic):** (peers/5) × min(2, |avg|/threshold) / 2.

## Custom BTC / Custom ETH

The trigger fires when the coin's own move since the round opened is at least `up_threshold` or
at most −`down_threshold`. Extra gates:
- `min_confidence`. Confidence (heuristic) is |move| / threshold / 3, so 3× the threshold = 1.0.
- `cooldown_s`
- `max_trades_per_window`

The BTC regime filter applies. Each config trades only its own coin.

## Pocket Aces [ref preset]

A directional trigger with a near-certainty band of 65–95¢. Confirm-mode DCA of $2 + $2 at 5¢
drops, an $8 cap, a 35¢ stop, and quiet hours 14–17 and 21–22 UTC. The reference claimed "up to
80% win rate". Note that at an average price of about 85¢, break-even is about 85%.

## BTC Mean Reversion (our design; the reference only had the regime filter)

z = (lead coin's round move) / (60s realised vol × √(elapsed minutes)). When |z| ≥ `zscore_entry`
and the 60s vol is inside `vol_min_pct`–`vol_max_pct`, it buys the **opposite** side on the target
coins. The RSI confirmation (15s samples) is optional. The lead coin is BTC, or BTC + ETH.

## AI Strat (our model; the reference's was private)

- **Inputs (16):**
  - the coin's round move; returns over 5, 15, 30 and 60s; 60s volatility; time into the round
  - the UP/DOWN asks, their sum, and the market's implied P(UP)
  - the other five coins' average move and the share of them moving up
  - BTC's round move and 1h change
- **Training data:** every settled round, sampled at 45, 60, …, 210s. Features come from stored 1s bars and use only data from at or before each sample second. Asks come from the latest book snapshot no more than 6s old. The label is the settled winner.
- **Model:** a 60/20/20 split by round time (no round straddles two splits). Logistic regression or gradient boosting, with Platt calibration fitted on the middle 20%.
- **Activation rule:** a model goes live only if its test log-loss beats the market's own prices on the held-out rounds. If it can't out-predict the crowd, it has no edge, and AI Strat stays silent.
- **Live use:** a trade needs P ≥ `min_probability` (0.60) **and** P − ask ≥ `min_edge` (3¢). Every prediction is logged to `model_predictions` with its probability, EV per $1 and risk class.
- **Retraining:** automatic every 6 hours, or on demand from Settings → AI Strat → Train now. It needs at least 60 settled rounds of history, about 5 hours of the backend running.
- **No DCA [ref]:** the reference's own data showed averaged-down positions lost 20% per trade.

## Scoring (what "hit rate" means here)

Each signal is scored against the settled winner at the ask seen when it fired, per $1, before fees:
- a win pays (1 − price) / price
- a loss pays −1

**Break-even hit rate = the average price paid.** A 60% hit rate on 70¢ tokens loses money. The
scorecard shows hit rate, break-even, edge and P&L per $1 side by side. It also shows how often
each *skip* reason would have won, so you can see whether a gate protects you or costs you.

## DCA planner (`engine/dca.py`, executed in Phase 6)

- **Stages** are dollar amounts.
- **Triggers:** `price` (first entry − k × step), `percent` (first entry × (1 − k × step)) or `signal` (the trigger is still firing).
- **Modes:** off, capped (one top-up), standard, or confirm (spot must still agree with the side).
- **Blocked when:** the total would pass `max_total`, the new average would pass `max_avg_price`, the last buy was under `min_seconds_between` ago, or fewer than `halt_seconds_before_close` (110s [ref]) remain.
- Every add still passes the shared risk engine.
