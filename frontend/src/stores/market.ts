import { create } from "zustand";
import { ASSETS, type Asset } from "@/lib/utils";
import { useAuth } from "@/stores/auth";

/** Live market state, streamed from OUR backend (`/ws/markets`, 1 frame per second).
 *  The backend owns the Binance and Polymarket connections; the browser talks only to it. */

export interface Candle { time: number; open: number; high: number; low: number; close: number }
export interface BookSummary { bid: number | null; ask: number | null; spread: number | null; ask_depth: number; bid_depth: number;
  last_trade: number | null; age_s: number | null; synced: boolean }
export interface MarketRef { id: string; slug: string; question: string; start: number; end: number; up: BookSummary | null; down: BookSummary | null }
export interface Features { ts: number; price: number | null; window_open: number | null; window_delta: number | null; secs_in: number;
  vol_60s: number | null; ret_1h: number | null; [k: `ret_${number}s`]: number | null }

export interface SpotState {
  price: number | null;
  change24h: number | null;
  change1h: number | null;
  windowOpen: number | null;
  windowDelta: number | null;
  features: Features | null;
  market: MarketRef | null;
  candles: Candle[];         // 1-minute candles: history from REST, live-updated from 1s bars
}

export interface FeedStatus { name: string; status: "live" | "stale" | "down" | "connecting"; last_msg_age_s: number | null; reconnects: number; last_error: string | null }

interface MarketStore {
  spot: Record<Asset, SpotState>;
  status: "connecting" | "live" | "stale" | "down";
  lastTick: number;
  serverOffset: number;      // serverTime - clientTime (s), for countdowns
  window: { start: number; end: number } | null;
  feeds: Record<string, FeedStatus>;
  start: () => void;
  loadCandles: (a: Asset) => Promise<void>;
}

const blank = (): SpotState => ({ price: null, change24h: null, change1h: null, windowOpen: null, windowDelta: null, features: null, market: null, candles: [] });

let ws: WebSocket | null = null;
let started = false;
let retry = 0;

type Set = (p: Partial<MarketStore> | ((s: MarketStore) => Partial<MarketStore>)) => void;

export const useMarket = create<MarketStore>((set, get) => ({
  spot: Object.fromEntries(ASSETS.map((a) => [a, blank()])) as Record<Asset, SpotState>,
  status: "connecting",
  lastTick: 0,
  serverOffset: 0,
  window: null,
  feeds: {},
  start: () => {
    if (started) return;
    started = true;
    connect(set);
    ASSETS.forEach((a) => void get().loadCandles(a));
    let tick = 0;
    setInterval(() => {
      // history may be empty right after a backend restart: retry until it arrives
      if (++tick % 5 === 0) ASSETS.forEach((a) => { if (get().spot[a].candles.length < 10) void get().loadCandles(a); });
      const age = Date.now() - get().lastTick;
      if (get().status === "live" && age > 5_000) set({ status: "stale" });
      if (get().status === "stale" && age > 20_000) ws?.close();   // force a reconnect
    }, 1000);
  },
  loadCandles: async (a) => {
    try {
      const token = useAuth.getState().token;
      const r = await fetch(`/api/markets/candles/${a}?interval=60&limit=120`, { headers: { Authorization: `Bearer ${token}` } });
      if (!r.ok) return;
      const candles: Candle[] = await r.json();
      set((s) => ({ spot: { ...s.spot, [a]: { ...s.spot[a], candles: mergeCandles(candles, s.spot[a].candles) } } }));
    } catch { /* stream status shows connectivity problems */ }
  },
}));

/** History first, then any live candles newer than it. */
function mergeCandles(history: Candle[], live: Candle[]): Candle[] {
  const last = history.at(-1)?.time ?? 0;
  return [...history, ...live.filter((c) => c.time > last)].slice(-180);
}

/** Fold a closed 1s bar into the 1-minute candle series. */
export function foldBar(candles: Candle[], bar: [number, number, number, number, number]): Candle[] {
  const [ts, o, h, l, c] = bar;
  const t = Math.floor(ts / 60) * 60;
  const last = candles.at(-1);
  if (last && last.time === t) {
    return [...candles.slice(0, -1), { time: t, open: last.open, high: Math.max(last.high, h), low: Math.min(last.low, l), close: c }];
  }
  if (last && t < last.time) return candles;
  return [...candles, { time: t, open: o, high: h, low: l, close: c }].slice(-180);
}

interface Frame {
  type: "markets"; ts: number; window: { start: number; end: number; secs_in: number };
  assets: Record<Asset, { price: number | null; change_24h: number | null; bar: [number, number, number, number, number] | null;
    features: Features; market: MarketRef | null }>;
  feeds: Record<string, FeedStatus>;
}

function connect(set: Set) {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws/markets`);
  set({ status: "connecting" });
  ws.onopen = () => ws?.send(JSON.stringify({ type: "auth", token: useAuth.getState().token }));
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.type === "ready") { retry = 0; set({ serverOffset: msg.server_time - Date.now() / 1000 }); return; }
    if (msg.type === "error") { set({ status: "down" }); return; }
    if (msg.type === "heartbeat") { set({ lastTick: Date.now() }); return; }
    if (msg.type !== "markets") return;
    const f = msg as Frame;
    set((s) => {
      const spot = { ...s.spot };
      for (const a of ASSETS) {
        const x = f.assets[a];
        if (!x) continue;
        const cur = spot[a];
        spot[a] = {
          ...cur, price: x.price, change24h: x.change_24h, change1h: x.features?.ret_1h ?? null,
          windowOpen: x.features?.window_open ?? null, windowDelta: x.features?.window_delta ?? null,
          features: x.features ?? null, market: x.market,
          candles: x.bar ? foldBar(cur.candles, x.bar) : cur.candles,
        };
      }
      const binance = f.feeds?.binance?.status;
      return { spot, window: f.window, feeds: f.feeds, lastTick: Date.now(),
               status: binance === "live" ? "live" : binance === "stale" ? "stale" : "down" };
    });
  };
  ws.onclose = (ev) => {
    set({ status: "down" });
    if (ev.code === 4401) { useAuth.getState().logout(); return; }
    const wait = Math.min(30_000, 1000 * 2 ** retry++);
    setTimeout(() => connect(set), wait);
  };
  ws.onerror = () => ws?.close();
}

export function windowDelta(s: SpotState): number | null {
  return s.windowDelta;
}
