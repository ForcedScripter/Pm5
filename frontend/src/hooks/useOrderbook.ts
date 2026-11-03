import { useQuery } from "@tanstack/react-query";
import { api } from "@/services/api";

export interface Level { price: number; size: number }
export interface Book { bids: Level[]; asks: Level[]; bestBid: number | null; bestAsk: number | null; spread: number | null; depthUsd: number }
type Raw = { bids: [number, number][]; asks: [number, number][]; bid: number | null; ask: number | null; spread: number | null; ask_depth: number };

const toBook = (r: Raw): Book => ({
  bids: r.bids.map(([price, size]) => ({ price, size })), asks: r.asks.map(([price, size]) => ({ price, size })),
  bestBid: r.bid, bestAsk: r.ask, spread: r.spread, depthUsd: r.ask_depth,
});

/** Depth for one market from our backend (it maintains the books from Polymarket's WS). */
export function useOrderbook(marketId: string | undefined) {
  return useQuery({
    queryKey: ["orderbook", marketId],
    enabled: !!marketId,
    refetchInterval: 2000,
    retry: false,
    queryFn: async () => {
      const r = await api<{ UP: Raw; DOWN: Raw }>(`/api/markets/${marketId}/orderbook?depth=10`);
      return { up: toBook(r.UP), down: toBook(r.DOWN) };
    },
  });
}
