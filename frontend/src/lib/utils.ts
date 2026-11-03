import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export const cn = (...v: ClassValue[]) => twMerge(clsx(v));

export const ASSETS = ["btc", "eth", "sol", "xrp", "doge", "bnb"] as const;
export type Asset = (typeof ASSETS)[number];

export const ASSET_META: Record<Asset, { sym: string; name: string; color: string; binance: string; glyph: string }> = {
  btc: { sym: "BTC", name: "Bitcoin", color: "#f7931a", binance: "BTCUSDT", glyph: "₿" },
  eth: { sym: "ETH", name: "Ethereum", color: "#8c9eff", binance: "ETHUSDT", glyph: "Ξ" },
  sol: { sym: "SOL", name: "Solana", color: "#14f195", binance: "SOLUSDT", glyph: "◎" },
  xrp: { sym: "XRP", name: "Ripple", color: "#22d3ee", binance: "XRPUSDT", glyph: "✕" },
  doge: { sym: "DOGE", name: "Dogecoin", color: "#c2a633", binance: "DOGEUSDT", glyph: "Ð" },
  bnb: { sym: "BNB", name: "BNB", color: "#f3ba2f", binance: "BNBUSDT", glyph: "◆" },
};

export const WINDOW_S = 300;
export const windowStart = (t = Date.now() / 1000) => Math.floor(t / WINDOW_S) * WINDOW_S;

export function fmtPrice(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return "—";
  const d = v >= 1000 ? 2 : v >= 1 ? 3 : v >= 0.1 ? 4 : 5;
  return v.toLocaleString(undefined, { minimumFractionDigits: d > 2 ? 2 : d, maximumFractionDigits: d });
}
export const fmtUsd = (v: number | null | undefined, sign = false) =>
  v == null || !Number.isFinite(v) ? "—" : `${sign ? (v >= 0 ? "+" : "−") : v < 0 ? "−" : ""}$${Math.abs(v).toFixed(2)}`;
export const fmtPct = (v: number | null | undefined, digits = 2) =>
  v == null || !Number.isFinite(v) ? "—" : `${v >= 0 ? "+" : "−"}${Math.abs(v).toFixed(digits)}%`;
export const fmtCents = (v: number | null | undefined) => (v == null ? "—" : `${(v * 100).toFixed(1)}¢`);
export const tone = (v: number | null | undefined) => (v == null || v === 0 ? "text-tx" : v > 0 ? "text-up" : "text-down");
export const mmss = (s: number) => `${Math.floor(Math.max(0, s) / 60)}:${String(Math.floor(Math.max(0, s) % 60)).padStart(2, "0")}`;
