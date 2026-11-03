"""Strategy configuration: defaults, presets and persistence.

The preset numbers mirror the reference console's settings page (MODE_PRESETS,
DCA_PRESETS and the "Pocket Aces" community preset) so the knobs mean the same
thing here as they did there.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

DATA_DIR = Path(os.getenv("DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
CONFIG_PATH = DATA_DIR / "config.json"

ALL_ASSETS = ["btc", "eth", "sol", "xrp", "doge", "bnb"]

# Entry gates. upThreshold / downThreshold are the % move of the spot price since
# the 5-minute window opened that counts as a directional signal.
MODE_PRESETS = {
    "selective":  {"upThreshold": 0.08,  "downThreshold": 0.14, "entryLow": 0.42, "entryHigh": 0.62, "maxConcurrent": 2},
    "balanced":   {"upThreshold": 0.055, "downThreshold": 0.10, "entryLow": 0.38, "entryHigh": 0.68, "maxConcurrent": 3},
    "aggressive": {"upThreshold": 0.015, "downThreshold": 0.03, "entryLow": 0.35, "entryHigh": 0.75, "maxConcurrent": 4},
}

# Averaging-down sizing. "confirm" only adds while spot still agrees with the side.
DCA_PRESETS = {
    "standard": {"dca1": 3, "dca2": 2.5, "maxPerTrade": 10.5},
    "off":      {"dca1": 0, "dca2": 0,   "maxPerTrade": 5},
    "capped":   {"dca1": 2, "dca2": 0,   "maxPerTrade": 7},
    "confirm":  {"dca1": 3, "dca2": 2.5, "maxPerTrade": 10.5},
}

DEFAULTS = {
    "strategy": "custom",          # custom | aces | constellation
    "mode": "balanced",
    "dcaMode": "standard",
    "assets": ["btc"],
    "bankroll": 200.0,             # paper bankroll / sizing reference
    "initialBuy": 5.0,             # $ per first entry (Polymarket min order is 5 shares)
    "dca1": 3.0,
    "dca2": 2.5,
    "dcaDrop": 0.09,               # price drop from entry that triggers each DCA step
    "maxPerTrade": 10.5,           # $ cap per position including DCA
    "stopLoss": 0.20,              # sell everything if the token bid falls to this
    "upThreshold": 0.055,
    "downThreshold": 0.10,
    "entryLow": 0.38,
    "entryHigh": 0.68,
    "avoidLow": 0.0,
    "avoidHigh": 0.0,
    "maxConcurrent": 3,
    "tradeUp": True,
    "tradeDown": True,
    "skipHours": [],               # UTC hours with no new entries
    "streakReversal": False,
    "streakLength": 7,
    # constellation
    "minAgree": 4,
    "laggardGap": 0.15,
    # timing + safety (not in the reference UI; sensible guards for running it yourself)
    "minSecondsIn": 30,            # don't enter in the first N seconds of a window
    "minSecondsLeft": 60,          # don't enter in the last N seconds
    "slippage": 0.02,              # max price above the ask we accept on a buy
    "maxDailyLoss": 50.0,          # bot stops itself after losing this much in a UTC day
}

# Fixed community preset ("Pocket Aces"): near-certain tokens only.
ACES_PRESET = {
    "strategy": "aces", "mode": "balanced", "dcaMode": "confirm",
    "entryLow": 0.65, "entryHigh": 0.95, "avoidLow": 0, "avoidHigh": 0,
    "dca1": 2, "dca2": 2, "dcaDrop": 0.05, "maxPerTrade": 8, "stopLoss": 0.35,
    "skipHours": [14, 15, 16, 17, 21, 22], "streakReversal": False,
}

_lock = threading.Lock()


def _clean(cfg: dict) -> dict:
    out = dict(DEFAULTS)
    for k, v in cfg.items():
        if k not in DEFAULTS:
            continue
        d = DEFAULTS[k]
        try:
            if isinstance(d, bool):
                out[k] = bool(v)
            elif isinstance(d, float):
                out[k] = float(v)
            elif isinstance(d, int):
                out[k] = int(v)
            elif isinstance(d, list):
                out[k] = list(v)
            else:
                out[k] = str(v)
        except (TypeError, ValueError):
            pass
    out["assets"] = [a for a in (x.lower() for x in out["assets"]) if a in ALL_ASSETS] or ["btc"]
    out["skipHours"] = sorted({int(h) for h in out["skipHours"] if 0 <= int(h) <= 23})
    out["streakLength"] = max(2, min(50, out["streakLength"]))
    out["maxConcurrent"] = max(1, min(6, out["maxConcurrent"]))
    out["minAgree"] = max(2, min(6, out["minAgree"]))
    if out["strategy"] not in ("custom", "aces", "constellation"):
        out["strategy"] = "custom"
    return out


def load() -> dict:
    with _lock:
        if CONFIG_PATH.exists():
            try:
                return _clean(json.loads(CONFIG_PATH.read_text()))
            except (OSError, json.JSONDecodeError):
                pass
        return dict(DEFAULTS)


def save(cfg: dict) -> dict:
    """Merge `cfg` over the current config, apply presets, persist and return it."""
    cur = load()
    cur.update(cfg)
    if cfg.get("strategy") == "aces":
        cur.update(ACES_PRESET)
    cleaned = _clean(cur)
    with _lock:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(cleaned, indent=2))
    return cleaned
