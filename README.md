# pm5bot

A trading bot for Polymarket's 5-minute crypto **Up/Down** markets (BTC, ETH, SOL, XRP, DOGE, BNB),
with a web console. It runs **on your own computer**: your wallet key stays on your machine,
encrypted, and the console is reachable only from that machine.

> ⚠️ It starts in **paper trading** (simulated orders on real live prices). No strategy here has
> a proven edge yet: Polymarket's taker fees mean you must win more often than the price you pay,
> and the backtest on real order books is still too small to judge (see `docs/backtesting.md`).
> Run it on paper for days before ever considering real money.

## Install (once)

1. Install **Docker Desktop** (Windows/Mac) from https://www.docker.com/products/docker-desktop/ and start it.
   - Windows: let it enable WSL 2 if it asks.
   - In Docker Desktop → Settings → General, tick **Start Docker Desktop when you sign in**, so the bot comes back after a reboot.
2. Install **Git** (https://git-scm.com/downloads) and download the code:
   ```bash
   git clone https://github.com/ForcedScripter/Pm5 pm5bot
   ```
   ```bash
   cd pm5bot
   ```
3. Run the setup script. It creates `.env` with random secrets, builds and starts everything, and prints your login. The first build takes about 5–10 minutes.
   - **Windows (PowerShell):**
     ```powershell
     powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
     ```
   - **Mac / Linux:**
     ```bash
     bash scripts/setup.sh
     ```
4. Open **http://localhost:8080** and sign in as `admin` with the printed password. It's also in `.env` as `ADMIN_PASSWORD`.

**Back up your `.env`** (for example to a USB stick or password manager). Its `MASTER_KEY` is the
only way to decrypt the wallet key you save in the console.

## Use it

1. **Settings → Strategy configuration:** enable one or more strategies and review their settings.
2. **Start bot** (top right): it trades on paper. Watch the **Terminal**, **History** and **Logs** pages.
3. **Backtest:** test strategies on history. Use **Recorded** data, which builds up while the bot runs (about 72 rounds per hour).
4. **Live trading** stays locked until you connect a wallet, set `ALLOW_LIVE_TRADING=true` in `.env`, and your paper results pass the checks in Settings → Live trading. Read `docs/execution.md` first.

The red **STOP** button halts everything immediately. The bot only runs while your PC is on and
awake: set Windows/macOS sleep to *Never* while plugged in if you want it running unattended.

## Everyday commands

Run these from the `pm5bot` folder:

| | Windows (PowerShell) | Mac / Linux |
|---|---|---|
| Update to the latest version | `powershell -ExecutionPolicy Bypass -File scripts\update.ps1` | `bash scripts/update.sh` |
| Back up the database | `powershell -ExecutionPolicy Bypass -File scripts\backup.ps1` | `bash scripts/backup.sh` |
| Stop (keeps all data) | `docker compose stop` | `docker compose stop` |
| Start again | `docker compose start` | `docker compose start` |
| See logs | `docker compose logs -f backend` | `docker compose logs -f backend` |

Updating keeps your trades, settings and wallet (they live in Docker volumes and `.env`).
**Don't delete `.env`:** the database password and wallet encryption key are in it.

## If something's wrong

- **Settings → Server status** shows the database, Redis and each live feed (Binance prices, Polymarket books, market discovery) with its last error.
- **"Docker is not running":** start Docker Desktop and wait for it to say *running*.
- **Port 8080 already in use:** change `"127.0.0.1:8080:8080"` in `docker-compose.yml` to, say, `"127.0.0.1:8090:8080"`, then open http://localhost:8090.
- **Feeds show "down":** check your internet connection. A VPN or firewall must allow `data-stream.binance.vision`, `clob.polymarket.com` and `gamma-api.polymarket.com`. Polymarket is not available in every country.

## Safety on your PC

- Everything listens on **127.0.0.1 only**, so other devices on your network can't reach the console or the database.
- The wallet key is encrypted with `MASTER_KEY` before it's stored. It's never shown again, never logged, and never sent anywhere except to sign orders with Polymarket's official SDK.
- Use a **dedicated wallet** with only what you can afford to lose. If you ever pasted a key into another website, consider that wallet compromised.

## Docs

| | |
|---|---|
| `docs/strategies.md` | What each strategy does and its settings |
| `docs/execution.md` | Orders, fees, risk limits, going live (checklist) |
| `docs/backtesting.md` | Backtesting, data quality, and why backfilled data is biased |
| `docs/architecture.md` | How it works inside |
| `docs/api_documentation.md` | API reference |
| `docs/reverse_engineering.md` | Notes on the reference app this was modelled on |

## For developers

Backend: Python 3.12+, FastAPI, Postgres, Redis (`backend/`). Frontend: React + Vite (`frontend/`).

```bash
cd backend && python -m pytest
```
```bash
cd frontend && npx vitest run
```

Live-feed checks: `RUN_LIVE=1 python -m pytest tests/live`.
