import type { Asset } from "@/lib/utils";

/** Constellation gate math, matching the reference's public /api/signals/live fields
 *  (docs/reverse_engineering.md §3.2). Pure so it is unit-tested and reused by the
 *  backend port in Phase 5. This is a live *preview*; orders come only from the engine. */
export interface ConsensusCfg {
  minConsensus: number;       // 4
  minSpotDeltaPct: number;    // 0.01: a coin counts as moving past this
  upThreshold: number;        // 0.055: group avg % for UP
  downThreshold: number;      // 0.10: group avg % for DOWN
  maxLaggardDeltaPct: number; // 0.15
  band: [number, number];     // [0.38, 0.68]
  firstCheck: number;         // 45 s
  lastEntry: number;          // 210 s
}

export const DEFAULT_CFG: ConsensusCfg = {
  minConsensus: 4, minSpotDeltaPct: 0.01, upThreshold: 0.055, downThreshold: 0.1,
  maxLaggardDeltaPct: 0.15, band: [0.38, 0.68], firstCheck: 45, lastEntry: 210,
};

export interface Candidate {
  asset: Asset; side: "UP" | "DOWN"; delta: number; price: number | null;
  gates: { consensus: boolean; laggard: boolean; band: boolean; timing: boolean }; pass: boolean;
}

export interface ConsensusResult {
  phase: "WARMUP" | "SCANNING" | "CLOSED";
  tally: { up: number; down: number; flat: number };
  up: { count: number; avg: number; met: boolean; assets: Asset[] };
  down: { count: number; avg: number; met: boolean; assets: Asset[] };
  side: "UP" | "DOWN" | null;
  candidates: Candidate[];
}

export function evaluate(
  deltas: Partial<Record<Asset, number | null>>,
  prices: Partial<Record<Asset, { up: number | null; down: number | null }>>,
  secsIn: number,
  cfg: ConsensusCfg = DEFAULT_CFG,
): ConsensusResult {
  const known = Object.entries(deltas).filter(([, d]) => d != null) as [Asset, number][];
  const ups = known.filter(([, d]) => d >= cfg.minSpotDeltaPct);
  const downs = known.filter(([, d]) => d <= -cfg.minSpotDeltaPct);
  const avg = (xs: [Asset, number][]) => (xs.length ? xs.reduce((s, [, d]) => s + d, 0) / xs.length : 0);
  const up = { count: ups.length, avg: avg(ups), met: false, assets: ups.map(([a]) => a) };
  const down = { count: downs.length, avg: avg(downs), met: false, assets: downs.map(([a]) => a) };
  up.met = up.count >= cfg.minConsensus && up.avg >= cfg.upThreshold;
  down.met = down.count >= cfg.minConsensus && down.avg <= -cfg.downThreshold;

  const phase = secsIn < cfg.firstCheck ? "WARMUP" : secsIn > cfg.lastEntry ? "CLOSED" : "SCANNING";
  const timing = phase === "SCANNING";
  // the stronger side leads if both qualify by count
  const lead = up.count >= down.count ? "UP" : "DOWN";
  const group = lead === "UP" ? up : down;
  const side = up.met ? "UP" : down.met ? "DOWN" : null;

  const candidates: Candidate[] = [];
  if (group.count >= cfg.minConsensus && group.count < known.length) {
    for (const [a, d] of known) {
      if (group.assets.includes(a)) continue;
      const directional = lead === "UP" ? d : -d;           // how far it has moved *with* the group
      const laggard = directional < cfg.minSpotDeltaPct && Math.abs(d) <= cfg.maxLaggardDeltaPct;
      const p = prices[a] ? (lead === "UP" ? prices[a]!.up : prices[a]!.down) : null;
      const band = p != null && p >= cfg.band[0] && p <= cfg.band[1];
      const consensus = lead === "UP" ? up.met : down.met;
      candidates.push({ asset: a, side: lead, delta: d, price: p, gates: { consensus, laggard, band, timing },
                        pass: consensus && laggard && band && timing });
    }
  }
  return { phase, tally: { up: ups.length, down: downs.length, flat: known.length - ups.length - downs.length },
           up, down, side, candidates };
}

/** Reference BTC regime filter: a BTC 1h change of ≥ +2% pulls the DOWN threshold to 0.06%;
 *  ≤ −2% pulls the UP threshold to 0.03%. */
export function btcRegime(change1h: number | null, cfg = { pct: 2, down: 0.06, up: 0.03 }) {
  if (change1h == null) return { state: "UNKNOWN" as const, upThreshold: null, downThreshold: null };
  if (change1h >= cfg.pct) return { state: "OVERHEATED" as const, upThreshold: null, downThreshold: cfg.down };
  if (change1h <= -cfg.pct) return { state: "OVERSOLD" as const, upThreshold: cfg.up, downThreshold: null };
  return { state: "NORMAL" as const, upThreshold: null, downThreshold: null };
}
