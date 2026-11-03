# pm5bot

A self-hosted trading bot for Polymarket's 5-minute crypto **Up or Down** markets
(BTC, ETH, SOL, XRP, DOGE, BNB), with a local web console.

Its strategy knobs are modeled on the settings an online "bot console" service exposed.
The difference: **your private key never leaves your machine.** It sits in `.env`, and only
the official [`py-clob-client-v2`](https://github.com/Polymarket/py-clob-client-v2) uses it,
to sign orders locally.

> ⚠️ Prediction-market trading can lose money fast. Past win rates (yours or anyone's)
> don't predict future ones. Run in paper mode first, then go live with small amounts.
> Polymarket restricts some countries. Make sure you're allowed to trade there.

## Quick start (paper trading, no key needed)

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
.venv/bin/python server.py
```

Open http://127.0.0.1:8787. Pick a strategy, save it, then press **Start bot**.
Paper mode fills against the **real live order book**, at the real prices, and settles
on Polymarket's real resolution. So paper P&L is a fair estimate (it can't model your
order moving the market).

## Going live

1. Use a **new wallet** that has never been pasted into any website. If you ever gave a key
   to an online bot service, assume that wallet is compromised. Move the funds out.
2. Log in to polymarket.com with that wallet and deposit USDC. Do one small manual trade so
   the token approvals get set up.
3. In `.env`:
   ```
   LIVE_TRADING=true
   PRIVATE_KEY=0x...            # Polymarket → Settings → Export private key
   FUNDER_ADDRESS=0x...         # your Polymarket deposit/profile address
   SIGNATURE_TYPE=1             # 1 = email/Magic login, 2 = MetaMask/browser wallet, 0 = plain EOA
   ```
4. Restart `server.py`. The header shows **LIVE**.
5. Winning shares pay out $1 each once the market resolves. If they don't show up as cash
   automatically, claim them with **Redeem** on polymarket.com.

## Strategies

| Tab | What it does |
|---|---|
| **Advanced** (`custom`) | Directional momentum. If spot (Binance) has moved past the UP/DOWN threshold (% since the window opened), it buys that side, as long as the token's price is inside the entry band and outside the avoid band. |
| **Constellation** | Multi-asset laggard. It watches all six assets. When at least *min agree* of them move the same way, it buys the selected market whose token for that side is the cheapest versus the group, by at least *laggard gap*. |
| **Pocket Aces** | A fixed preset. It only buys near-certain tokens (65¢–95¢), uses confirm-mode DCA of $2 + $2 at 5¢ drops, caps each position at $8, has a 35¢ stop, and stays quiet from 14–17 and 21–22 UTC. |

Settings shared by all three:
- **Mode** (selective / balanced / aggressive) fills in the thresholds and entry band.
- **DCA** (off / capped / standard / confirm) sets the averaging-down sizes. `confirm` only adds to a position while spot still agrees with your side.
- **Stop-loss** sells if the token's bid falls to the stop price. It won't sell more than 5¢ below the stop, or in the final 20 seconds. Otherwise it holds to resolution.
- **Streak reversal** takes the opposite side after N identical outcomes in a row.
- **Skip hours** blocks new entries during the UTC hours you pick.
- **Max concurrent** limits how many positions can be open at once.
- **Daily loss stop** shuts the bot off for the day after that loss.

The online service also had an "AI Strat", a model trained on its own private trade logs.
That model isn't public, so it isn't included here.

Polymarket settles these markets on a **Chainlink 60-second average price**. The bot uses
Binance spot as its fast signal, so very small moves near the end of a window can disagree
with the final result. That's why entries stop 60 seconds before the close by default
(`minSecondsLeft` in `data/config.json`).

## Running 24/7 on a server

Set `HOST=0.0.0.0`, a long `DASHBOARD_TOKEN`, and `AUTOSTART=true`. Then run it under systemd
or `tmux`. Better still, keep `HOST=127.0.0.1` and reach the console through an SSH tunnel:
`ssh -L 8787:127.0.0.1:8787 you@server`.

## Files

- `bot/feeds.py`: Polymarket market discovery, order books, resolution, and Binance prices
- `bot/engine.py`: the strategy loop, DCA, stop-loss and settlement
- `bot/executor.py`: paper fills, and live orders through `py-clob-client-v2`
- `bot/config.py`: defaults and presets (saved to `data/config.json`)
- `server.py` and `static/index.html`: the local console
