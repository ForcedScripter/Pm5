# pm5bot

A trading bot for Polymarket's 5-minute crypto **Up/Down** markets (BTC, ETH, SOL, XRP, DOGE, BNB),
with a web console. It runs **on your own computer**: your wallet key stays on your machine,
encrypted, and the console is reachable only from that machine.

> ⚠️ It starts in **paper trading** (simulated orders on real live prices). No strategy here has
> a proven edge yet: Polymarket's taker fees mean you must win more often than the price you pay,
> and the backtest on real order books is still too small to judge (see `docs/backtesting.md`).
> Run it on paper for days before ever considering real money.

## Quick Start: Run Locally on Windows (No Docker)

If you don't use Docker or prefer running directly on your system:

1. Run the local setup script in PowerShell:
   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\setup-local.ps1
   ```
   *This automatically initializes local PostgreSQL in `.pgdata`, sets up the Python virtual environment (`.venv`), installs all backend dependencies with in-memory Redis fallback, compiles the React frontend into `frontend/dist`, and prints your admin login.*

2. Start the local server:
   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\start-local.ps1
   ```
3. Open **http://localhost:8000** and sign in as `admin`.
4. When done, stop background services:
   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\stop-local.ps1
   ```

---

## Alternative: Run with Docker Desktop

1. Install **Docker Desktop** (Windows/Mac) from https://www.docker.com/products/docker-desktop/ and start it.
2. Run the Docker setup script:
   - **Windows (PowerShell):**
     ```powershell
     powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
     ```
   - **Mac / Linux:**
     ```bash
     bash scripts/setup.sh
     ```
3. Open **http://localhost:8080** and sign in as `admin`.

---

## Use it

1. **Settings → Strategy configuration:** enable one or more strategies and review their settings.
2. **Start bot** (top right): it trades on paper. Watch the **Terminal**, **History** and **Logs** pages.
3. **Backtest:** test strategies on history. Use **Recorded** data, which builds up while the bot runs (about 72 rounds per hour).
4. **Live trading** stays locked until you connect a wallet, set `ALLOW_LIVE_TRADING=true` in `.env`, and your paper results pass the checks in Settings → Live trading. Read `docs/execution.md` first.

The red **STOP** button halts everything immediately. The bot only runs while your PC is on and
awake: set Windows/macOS sleep to *Never* while plugged in if you want it running unattended.

## Everyday commands

| | Local (No Docker) | Docker |
|---|---|---|
| Start instance | `powershell -File scripts\start-local.ps1` | `docker compose start` |
| Stop instance | `powershell -File scripts\stop-local.ps1` | `docker compose stop` |
| Backup database | `powershell -File scripts\backup.ps1` | `powershell -File scripts\backup.ps1` |
| Update code | `git pull` | `powershell -File scripts\update.ps1` |

## Running the tests

The backend tests need a Postgres database named `pm5_test` (point `TEST_DATABASE_URL` at it if it
isn't on `localhost:5432`, e.g. the local server from `setup-local.ps1` on port 5433):

```powershell
.\.venv\Scripts\pip install -r backend\requirements-dev.txt
cd backend
$env:TEST_DATABASE_URL = "postgresql+asyncpg://postgres@127.0.0.1:5433/pm5_test"
..\.venv\Scripts\python -m pytest -q --ignore=tests/live
cd ..\frontend; npx vitest run; npx tsc -b
```

## Safety on your PC

- Everything listens on **127.0.0.1 only**, so other devices on your network can't reach the console or the database.
- The wallet key is encrypted with `MASTER_KEY` before it's stored. It's never shown again, never logged, and never sent anywhere except to sign orders with Polymarket's official SDK.
- The server refuses to start if `MASTER_KEY` is missing or `JWT_SECRET` is shorter than 32 characters.
- **Keep this folder out of OneDrive, Dropbox and similar sync tools.** `.env` holds `MASTER_KEY` and `JWT_SECRET`; a synced copy puts them in the cloud. Back up `MASTER_KEY` offline instead.
- After your first login you can delete the `ADMIN_PASSWORD` line from `.env`; it is only read when the account is first created.
- New local installs give the Postgres database a random password (stored in `%LOCALAPPDATA%\pm5bot\pg.password`). An install made before this change still uses no password; to switch, back up, delete `%LOCALAPPDATA%\pm5bot\pgdata`, and run `setup-local.ps1` again.
- Use a **dedicated wallet** with only what you can afford to lose. If you ever pasted a key into another website, consider that wallet compromised.
