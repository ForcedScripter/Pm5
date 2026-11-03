# Phase 1 — Reference investigation: polexbot.fun

Investigated 2026-11-03 from public pages only. I used a headless Chromium and plain HTTP
GETs. I did not log in or connect a wallet, and I didn't call any private or POST endpoint.
Raw evidence is in `docs/reference/`: screenshots (desktop and mobile), `inspection.json`
(DOM, buttons, inputs and network requests per page), and `signals_live.json` and
`signals_history.json` (the site's public signal feed).

## 0. Findings that change how this project should be run

1. **The site collects private keys.** Settings → Wallet has a "Key" field ("Reveal Magic
   key… paste it above"). Its JavaScript POSTs `{privateKey, proxyAddress}` to
   `/api/wallet/connect` and `/api/wallet/derive-address` on the site's own server. Whoever
   operates that server can drain any wallet entered there.
2. **Its domains are on a public phishing blocklist.** `polexbot.fun`, `.live`, `.net`, `.pro`
   and `.xyz` appear in `phishdestroy/destroylist`, added between October 24 and November 1, 2026.
3. **Its advertised numbers contradict its own data.** The settings page shows "Avg win rate
   79.9%", "Earned by traders +$3,994,673" and "Pocket Aces… up to 80%". But the site's own
   public endpoint `/api/signals/live` → `stats` reports **641 resolved signals, 344 wins, a
   53.7% hit rate**. The last 50 signals in `/api/signals/history` went 23 wins out of 50
   (46%), at a median token price of $0.53. A 53.7% win rate at about 53¢ per share is
   roughly break-even before fees.
   → Do not assume the reference strategy is profitable. This replica treats every strategy
   as unproven until our own paper trading and out-of-sample backtests say otherwise.
4. **No source code is public.** GitHub has nothing under "polexbot". The "5MinuteBot" repo
   (`nazeer991/5MinuteBot`) is empty. All trading logic runs server-side, so everything below
   is reconstructed from what the client and the public JSON show.

## 1. Navigation

| Page | Route | Notes |
|---|---|---|
| Landing | `/` | Marketing, tutorial videos, "Live Market Pulse", strategy explainers, plus off-topic politics, esports and UEFA market pages |
| Terminal | `/app` | Main console (the "TERMINAL" tab) |
| Settings | `/settings` | Wallet, fees, platform stats, AI onboarding, strategy configuration |
| Logs | (tab in the console) | Filters: ALL / TRADES / SKIPS / ERRORS |
| Signals | `/signals` | Public "Signal Desk": six-coin agreement, buyable candidates, recent signals |
| Extras | `/live`, `/calculator`, `/replay`, `/blog`, `/brief` | Marketing and analytics pages; out of scope |

## 2. Terminal (`/app`) layout

See `reference/app.png`.
- **Top ticker** scrolls price and 24h % for all six assets.
- **Header**: logo, a "v1.1 · live mode" badge, a pill nav (Terminal / Settings / Logs /
  Signals), a "Market feed · last tick Ns ago" health pill, a Link Wallet button and a gear icon.
- **Left column**:
  - Watchlist ("6 LIVE"): coin, name, sparkline, price, 24h %
  - BTC Regime (lead): BTC price, 1h change, filter state (NORMAL / …), a gradient meter, and this explainer: *"A BTC 1h change of +2% or more pulls the DOWN threshold to 0.06%; −2% or less pulls the UP threshold to 0.03%."*
  - Your Numbers: total P&L, trades placed, won vs lost
- **Centre**: a KPI strip (Bankroll with "pUSD + positions", Total P&L, Trades, Win rate, BTC
  1h filter, Resolve src) and a large BTC/USD 5-minute candle chart with an asset switcher.
- **Right column**: Order Book for the selected asset, Positions Open, and an Activity Log
  (expandable).
- **Footer**: data source ("Prices from CoinGecko"), last tick, a status message, and the
  disclaimer "EXPERIMENTAL · NOT FINANCIAL ADVICE".
- **Mobile**: one column; the pill nav wraps under the logo; KPIs become a 3×2 grid.

**Design tokens** (from `k2.css` and inline `:root`): canvas `#24211a` / `#14120e`; surfaces
`#2e2a21`, `#38332a`, `#594d38`; amber accent `#f79240`; text `#f9f6f0` / `#cebea2`; warm
1px borders `rgba(198,178,140,.15–.36)`. Fonts are Fredoka (brand), Nunito (UI) and Space
Grotesk (numbers). Cards are glassy with rounded 16–20px corners, gold-gradient active pills,
and a pink-to-orange gradient on the wallet CTA.

## 3. Settings (`/settings`)

- **Wallet**: private-key field, "developer (proxy) address" field, and Link Wallet. ⚠ This is the key-collection flow. **It will not be reproduced.**
- **Platform fees**: "$0.03 per winning trade" or "$0.015 per trade". Out of scope, since there's no platform operator.
- **Platform statistics**: marketing counters (see §0.3).
- **"Describe your style — let AI set everything up"**: a free-text prompt plus three example chips, which maps the text to a config through `/api/onboard-ai`.
- **Strategy configuration** tabs: Constellation (recommended), AI Strat (AI-built), Pocket Aces (user-built), Simple, Advanced. Also "Ask AI", "Reset defaults" and a "Running right now with —" banner.

### 3.1 Parameters observed (exact values from client JS and the public API)

| Group | Key | Default / presets |
|---|---|---|
| Mode | `upThreshold`, `downThreshold`, `entryLow`, `entryHigh`, `maxConcurrent` | selective 0.08/0.14/0.42/0.62/2 · balanced 0.055/0.10/0.38/0.68/3 · aggressive 0.015/0.03/0.35/0.75/4 |
| DCA | `dcaMode`, `dca1`, `dca2`, `maxPerTrade` | standard 3/2.5/10.5 · off 0/0/5 · capped 2/0/7 · confirm 3/2.5/10.5 |
| DCA trigger | `dcaDrop` | 0.09 (Constellation default); "buying halted 110s before close" |
| Sizing | `initialBuy`, `bankroll` | 5, 200 |
| Exit | `stopLoss` | 0.20 |
| Avoid band | `avoidLow`, `avoidHigh` | 0 / 0 (off) |
| Direction | `tradeUp`, `tradeDown` | both on |
| Hours | `skipHours` | UTC hours, chosen on a 0–23 grid colored by BTC hourly volatility |
| Streak | `streakReversal`, `streakLength` | off, 7 (2–50) |
| Assets | `assets` | btc / eth / both (Simple and Advanced); all six (Constellation) |
| Constellation | `minAgree`, `laggardGap` | 4, 0.15 |
| Pocket Aces | fixed | 0.65–0.95 band, confirm DCA $2/$2 @ 5¢, $8 cap, 0.35 stop, skip 14–17, 21–22 UTC |
| AI Strat | `initialBuy`, `maxConcurrent`, `bankroll`, `minAgree`, `skipHours` | Entry logic is in a server-side model file (`data/ai_strat_model.json`). Not public. |

### 3.2 Constellation engine, from `/api/signals/live`

```
window: firstCheck = 45s, lastEntry = 210s (into the 300s round)
cfg:    minConsensus = 4, minSpotDeltaPct = 0.01, maxLaggardDeltaPct = 0.15, band = [0.38, 0.68]
per asset: delta = (spot − open) / open × 100, dir = UP/DOWN/flat
side UP   if ≥ minConsensus assets have delta ≥ +minSpotDeltaPct and their avg ≥ upThreshold (0.055)
side DOWN if ≥ minConsensus assets have delta ≤ −minSpotDeltaPct and their avg ≤ −downThreshold (0.10)
candidate = an asset outside the consensus group (its |delta| < minSpotDeltaPct in that direction)
gates: consensus ✓, laggard (|laggardDelta| ≤ maxLaggardDeltaPct) ✓, band (side token ask ∈ band) ✓
output: confidence ∈ [0.17, 0.63] (formula not observable), boost (always null in sample)
```

From the last 50 signals: secsIn ranged 45–206 (median 99.5), consensusCount 4–5,
laggardDelta between −0.094% and +0.010%, and token price 0.385–0.675. The landing page
also says "Agreement scan at T+90s" and "Analyze (T+90–150s)".

### 3.3 Other mechanics described publicly
- BTC regime filter: thresholds shift based on BTC's 1h change (±2%). See §2.
- "Settled P&L, never estimated": realized P&L comes from the Polymarket Data API's closed-positions endpoint.
- Resolution source: Chainlink Data Streams 60-second TWAP for each asset (from the Gamma `resolutionSource` field).
- Data sources the client uses: a Binance REST ping, the Binance klines and combined-stream WebSocket, and CoinGecko through a server proxy (`/api/cg/...`).

## 4. Feature-parity matrix

| Reference feature | Observed behaviour | Reconstructed implementation | Status | Unknown / unavailable |
|---|---|---|---|---|
| Market discovery (6 assets) | Slugs `{asset}-updown-5m-{start}` | Backend registry: current + next window, rollover, resolution tracking, stored in PG | ✅ Phase 4 (live-verified) | – |
| Live spot feed | Binance WS / CoinGecko | Backend Binance collector (1s bars, book ticker, 24h) with REST backfill; stored in PG | ✅ Phase 4 | – |
| Order books | Right-panel order book | Backend CLOB WS books (snapshot + deltas, live-verified), sampled to PG every 5s | ✅ Phase 4 | – |
| Constellation | §3.2 | Same gates and parameters | ⏳ Phase 5 | Confidence formula, "boost" |
| AI Strat | Weekly-retrained calibration model | Our own logistic regression + gradient boosting, trained only on our logged signals (chronological splits) | ⏳ Phase 5 | Their model and data (private) |
| BTC & ETH rules (Simple/Advanced) | Momentum vs thresholds, band, hours, streak | Same | ✅ v0 | – |
| BTC regime filter | ±2% 1h shifts thresholds | Same rule, plus optional z-score/vol extensions (the spec's "Mean Reversion") | ⏳ Phase 5 | Exact NORMAL/other state names |
| Pocket Aces | Fixed preset | Same | ✅ v0 | – |
| DCA (off/capped/standard/confirm) | $5 → +$3 → +$2.50 at 9¢ drops; halt 110s before close | Same, behind the shared risk engine | ✅ v0 (halt rule ⏳) | – |
| Stop-loss | 0.20 default | Same, with an anti-dump floor and no stops in the last 20s | ✅ v0 | Reference exit-order type |
| Streak reversal | N same outcomes → fade | Same | ✅ v0 | – |
| Logs (All/Trades/Skips/Errors) | Activity log | `/logs` page + `GET /api/logs` with category filters and paging (engine writes entries from Phase 5) | ✅ Phase 3 | – |
| Signal Desk | Public live and history JSON | `/signals`: live six-coin gate preview from real prices + your saved Constellation config; logged history in Phase 5 | ✅ preview / ⏳ history | – |
| Settled P&L | Data API closed positions | Reconcile against the Data API `/positions` and `/closed-positions` | ⏳ Phase 6 | – |
| Wallet link | **Private key POSTed to *their* server** | Same in-website flow (key + Polymarket address), but it POSTs only to **your own** backend, which encrypts it at rest (Fernet/MASTER_KEY) and never returns or logs it. See architecture.md | ✅ Phase 2 | – |
| AI onboarding / Ask AI | Free text → config | Optional; needs an LLM API key | ⏳ optional | Their prompt |
| Platform fees / platform stats | Marketing | Not applicable (self-hosted) | 🚫 | – |
| Visual design | §2 tokens and layout | Same layout (ticker, pill nav, 3-column Terminal, tabbed strategy config), same token system, under our own name and logo. Screenshots: `docs/screenshots/` | ✅ Phase 3 | Their logo and brand assets (not copied) |

## 5. What v0 (`pm5bot`, already built and running in paper mode) proved

- Gamma discovery, CLOB book reads, Binance data and Gamma resolution all work live.
- Paper fills walk the real book. The first run exposed a stop-loss that dumped shares at
  $0.01 in the last seconds of a window. That's fixed now.
- Binance's 1-minute open doesn't equal Polymarket's Chainlink-TWAP reference, so small
  late-window moves can disagree with how the market settles.
