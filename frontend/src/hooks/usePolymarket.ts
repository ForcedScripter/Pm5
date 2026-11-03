import { useQuery } from "@tanstack/react-query";
import { ASSETS, windowStart, type Asset } from "@/lib/utils";

/** Current 5-minute Up/Down markets, discovered from Polymarket's public Gamma API,
 *  and their CLOB order books. Read-only public data; no auth involved. */
const GAMMA = "https://gamma-api.polymarket.com";
const CLOB = "https://clob.polymarket.com";

export interface PmMarket {
  asset: Asset;
  slug: string;
  question: string;
  start: number;
  end: number;
  upToken: string;
  downToken: string;
  conditionId: string;
  liquidity: number;
}

export interface Level { price: number; size: number }
export interface Book { bids: Level[]; asks: Level[]; bestBid: number | null; bestAsk: number | null; spread: number | null; depthUsd: number }

async function discover(asset: Asset, start: number): Promise<PmMarket | null> {
  const slug = `${asset}-updown-5m-${start}`;
  const r = await fetch(`${GAMMA}/events?slug=${slug}`);
  if (!r.ok) throw new Error(`gamma ${r.status}`);
  const ev = await r.json();
  const m = ev?.[0]?.markets?.[0];
  if (!m) return null;
  let [up, down] = JSON.parse(m.clobTokenIds) as string[];
  const outcomes = JSON.parse(m.outcomes) as string[];
  if (outcomes[0].toLowerCase() !== "up") [up, down] = [down, up];
  return { asset, slug, question: m.question, start, end: start + 300, upToken: up, downToken: down,
           conditionId: m.conditionId, liquidity: Number(m.liquidityClob ?? m.liquidity ?? 0) };
}

export function useWindowMarkets() {
  const start = windowStart();
  return useQuery({
    queryKey: ["pm-markets", start],
    queryFn: async () => {
      const res = await Promise.all(ASSETS.map((a) => discover(a, start).catch(() => null)));
      return Object.fromEntries(res.filter(Boolean).map((m) => [m!.asset, m!])) as Partial<Record<Asset, PmMarket>>;
    },
    staleTime: 60_000,
    refetchInterval: (q) => (Object.keys(q.state.data ?? {}).length < ASSETS.length ? 10_000 : false),
  });
}

export function parseBook(raw: { bids?: { price: string; size: string }[]; asks?: { price: string; size: string }[] }): Book {
  const bids = (raw.bids ?? []).map((x) => ({ price: +x.price, size: +x.size })).sort((a, b) => b.price - a.price);
  const asks = (raw.asks ?? []).map((x) => ({ price: +x.price, size: +x.size })).sort((a, b) => a.price - b.price);
  const bestBid = bids[0]?.price ?? null, bestAsk = asks[0]?.price ?? null;
  const depthUsd = asks.filter((l) => bestAsk != null && l.price <= bestAsk + 0.05).reduce((s, l) => s + l.price * l.size, 0);
  return { bids, asks, bestBid, bestAsk, spread: bestBid != null && bestAsk != null ? +(bestAsk - bestBid).toFixed(4) : null, depthUsd };
}

/** Books for every token of the current window (polled; Phase 4 moves this to the CLOB WS). */
export function useBooks(markets: Partial<Record<Asset, PmMarket>> | undefined, intervalMs = 3000) {
  const tokens = Object.values(markets ?? {}).flatMap((m) => [m!.upToken, m!.downToken]);
  return useQuery({
    queryKey: ["pm-books", tokens.join(",")],
    enabled: tokens.length > 0,
    refetchInterval: intervalMs,
    queryFn: async () => {
      const r = await fetch(`${CLOB}/books`, { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(tokens.map((token_id) => ({ token_id }))) });
      if (!r.ok) throw new Error(`clob ${r.status}`);
      const arr: { asset_id: string }[] = await r.json();
      return Object.fromEntries(arr.map((b) => [b.asset_id, parseBook(b as never)])) as Record<string, Book>;
    },
  });
}
