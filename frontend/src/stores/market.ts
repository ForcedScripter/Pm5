import { create } from "zustand";
import { ASSETS, ASSET_META, WINDOW_S, windowStart, type Asset } from "@/lib/utils";

/** Live spot data, straight from Binance's public market-data endpoints (real data only).
 *  Phase 4 replaces the direct browser feed with the backend's /ws/markets stream. */
const REST = "https://data-api.binance.vision/api/v3";
const WS = "wss://data-stream.binance.vision/stream";

export interface Candle { time: number; open: number; high: number; low: number; close: number }

export interface SpotState {
  price: number | null;
  change24h: number | null;     // %
  change1h: number | null;      // %
  windowOpen: number | null;    // 1m open at current 5-minute window start
  windowStartTs: number | null;
  candles: Candle[];            // last ~120 x 1m
}

interface MarketStore {
  spot: Record<Asset, SpotState>;
  status: "connecting" | "live" | "stale" | "down";
  lastTick: number;             // ms
  start: () => void;
}

const blank = (): SpotState => ({ price: null, change24h: null, change1h: null, windowOpen: null, windowStartTs: null, candles: [] });

let ws: WebSocket | null = null;
let started = false;
let retry = 0;

export const useMarket = create<MarketStore>((set, get) => ({
  spot: Object.fromEntries(ASSETS.map((a) => [a, blank()])) as Record<Asset, SpotState>,
  status: "connecting",
  lastTick: 0,
  start: () => {
    if (started) return;
    started = true;
    void loadHistory(set, get);
    connect(set, get);
    // window rollover + staleness watchdog
    let lastWindow = windowStart();
    setInterval(() => {
      const w = windowStart();
      if (w !== lastWindow) { lastWindow = w; void loadHistory(set, get); }
      const age = Date.now() - get().lastTick;
      if (get().status === "live" && age > 15_000) set({ status: "stale" });
      if (get().status === "stale" && age > 30_000) { ws?.close(); }
    }, 1000);
  },
}));

type Set = (p: Partial<MarketStore> | ((s: MarketStore) => Partial<MarketStore>)) => void;
type Get = () => MarketStore;

async function loadHistory(set: Set, _get: Get) {
  const ws0 = windowStart();
  await Promise.all(ASSETS.map(async (a) => {
    try {
      const r = await fetch(`${REST}/klines?symbol=${ASSET_META[a].binance}&interval=1m&limit=120`);
      const k: (string | number)[][] = await r.json();
      const candles: Candle[] = k.map((x) => ({ time: Number(x[0]) / 1000, open: +x[1], high: +x[2], low: +x[3], close: +x[4] }));
      const last = candles.at(-1)?.close ?? null;
      const hourAgo = candles.at(-61)?.open ?? null;
      const wo = candles.find((c) => c.time === ws0)?.open ?? null;
      set((s) => ({ spot: { ...s.spot, [a]: { ...s.spot[a], candles, price: s.spot[a].price ?? last,
        change1h: last && hourAgo ? ((last - hourAgo) / hourAgo) * 100 : null, windowOpen: wo, windowStartTs: ws0 } } }));
    } catch { /* surfaced through status if the WS is also down */ }
  }));
}

function connect(set: Set, get: Get) {
  const streams = ASSETS.flatMap((a) => { const s = ASSET_META[a].binance.toLowerCase(); return [`${s}@miniTicker`, `${s}@kline_1m`]; });
  ws = new WebSocket(`${WS}?streams=${streams.join("/")}`);
  set({ status: "connecting" });
  ws.onopen = () => { retry = 0; };
  ws.onmessage = (ev) => {
    const { stream, data } = JSON.parse(ev.data);
    const a = ASSETS.find((x) => stream.startsWith(ASSET_META[x].binance.toLowerCase()));
    if (!a) return;
    const now = Date.now();
    if (stream.endsWith("@miniTicker")) {
      const price = +data.c, open24 = +data.o;
      set((s) => ({ status: "live", lastTick: now, spot: { ...s.spot, [a]: { ...s.spot[a], price, change24h: ((price - open24) / open24) * 100 } } }));
    } else {
      const k = data.k;
      const c: Candle = { time: k.t / 1000, open: +k.o, high: +k.h, low: +k.l, close: +k.c };
      set((s) => {
        const cur = s.spot[a];
        const candles = cur.candles.length && cur.candles.at(-1)!.time === c.time ? [...cur.candles.slice(0, -1), c]
          : [...cur.candles, c].slice(-180);
        const ws0 = windowStart();
        const windowOpen = c.time === ws0 ? c.open : cur.windowStartTs === ws0 ? cur.windowOpen : null;
        const hourAgo = candles.at(-61)?.open;
        return { status: "live", lastTick: now, spot: { ...s.spot, [a]: { ...cur, candles, price: c.close, windowOpen,
          windowStartTs: ws0, change1h: hourAgo ? ((c.close - hourAgo) / hourAgo) * 100 : cur.change1h } } };
      });
    }
  };
  ws.onclose = () => {
    set({ status: get().lastTick ? "stale" : "down" });
    const wait = Math.min(30_000, 1000 * 2 ** retry++);
    setTimeout(() => connect(set, get), wait);
  };
  ws.onerror = () => ws?.close();
}

/** % move of spot since the current 5-minute window opened (the signal input the strategies use). */
export function windowDelta(s: SpotState): number | null {
  if (s.price == null || s.windowOpen == null) return null;
  return ((s.price - s.windowOpen) / s.windowOpen) * 100;
}

export const secondsIntoWindow = () => Date.now() / 1000 - windowStart();
export const secondsLeft = () => WINDOW_S - secondsIntoWindow();
