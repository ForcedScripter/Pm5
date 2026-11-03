import { useQuery } from "@tanstack/react-query";
import { motion } from "framer-motion";
import { Crosshair, History, Radar } from "lucide-react";
import { useNow } from "@/components/layout/Shell";
import { Badge, Empty, Panel, PanelHeader } from "@/components/ui/panel";
import { ASSETS, ASSET_META, cn, fmtCents, fmtPct, fmtPrice, mmss, tone, windowStart, type Asset } from "@/lib/utils";
import { Api } from "@/services/api";
import { useMarket, windowDelta } from "@/stores/market";
import { DEFAULT_CFG, evaluate, type ConsensusCfg } from "@/strategies/consensus";
import { SignalTable } from "@/strategies/SignalTable";
import { useSignalStream } from "@/hooks/useSignalStream";

/** Live six-coin consensus scan using the Constellation config you saved. Computed in the browser
 *  from real prices; it shows what the gates see. Orders are placed only by the engine (Phase 5). */
export default function Signals() {
  const now = useNow(1000);
  const live = useSignalStream();
  const spot = useMarket((s) => s.spot);
  const strat = useQuery({ queryKey: ["strategies"], queryFn: Api.strategies });
  const p = strat.data?.find((s) => s.key === "constellation")?.params as Record<string, any> | undefined; // eslint-disable-line @typescript-eslint/no-explicit-any
  const cfg: ConsensusCfg = p ? {
    minConsensus: p.min_consensus, minSpotDeltaPct: p.min_spot_delta_pct, upThreshold: p.up_threshold, downThreshold: p.down_threshold,
    maxLaggardDeltaPct: p.max_laggard_delta_pct, band: [p.entry_low, p.entry_high], firstCheck: p.window.first_check_s, lastEntry: p.window.last_entry_s,
  } : DEFAULT_CFG;

  const secsIn = now / 1000 - windowStart();
  const deltas = Object.fromEntries(ASSETS.map((a) => [a, windowDelta(spot[a])])) as Record<Asset, number | null>;
  const prices = Object.fromEntries(ASSETS.map((a) => {
    const m = spot[a].market;
    const current = m && m.start === windowStart();     // ignore last round's settled books at rollover
    return [a, { up: current ? m.up?.ask ?? null : null, down: current ? m.down?.ask ?? null : null }];
  })) as Record<Asset, { up: number | null; down: number | null }>;
  const r = evaluate(deltas, prices, secsIn, cfg);
  const lead = r.up.count >= r.down.count ? r.up : r.down;
  const leadSide = r.up.count >= r.down.count ? "UP" : "DOWN";
  const headline = r.side ? (r.candidates.some((c) => c.pass) ? `${lead.count} coins agree ${leadSide}, and a laggard is buyable`
    : `${lead.count} coins agree ${leadSide}, but nothing is buyable`) : `No consensus yet (${r.tally.up} up · ${r.tally.down} down · ${r.tally.flat} flat)`;

  return (
    <div className="space-y-5">
      <Panel gold className="p-6">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div>
            <Badge className={cn(r.phase === "SCANNING" ? "border-up/50 text-up" : "border-amber/50 text-amber")}>{r.phase} · {mmss(secsIn)} in · {mmss(300 - secsIn)} left</Badge>
            <motion.h1 key={headline} initial={{ opacity: 0, y: 4 }} animate={{ opacity: 1, y: 0 }} className="mt-3 font-brand text-3xl text-tx-hi">{headline}</motion.h1>
            <p className="mt-2 max-w-2xl text-[13px] text-tx-low">
              Gates from your saved Constellation config: ≥{cfg.minConsensus} coins moving past ±{cfg.minSpotDeltaPct}%, group average ≥ {cfg.upThreshold}% (UP) or ≤ −{cfg.downThreshold}% (DOWN),
              laggard within ±{cfg.maxLaggardDeltaPct}%, token ask in {cfg.band[0]}–{cfg.band[1]}, scanning {cfg.firstCheck}s–{cfg.lastEntry}s into the round.
            </p>
          </div>
          <div className="grid grid-cols-2 gap-3 text-center">
            <div className="glass px-5 py-3"><div className="panel-title !text-[10px]">UP group</div><div className={cn("num mt-1 text-xl font-semibold", r.up.met ? "text-up" : "text-tx")}>{r.up.count} · {fmtPct(r.up.avg, 3)}</div></div>
            <div className="glass px-5 py-3"><div className="panel-title !text-[10px]">DOWN group</div><div className={cn("num mt-1 text-xl font-semibold", r.down.met ? "text-down" : "text-tx")}>{r.down.count} · {fmtPct(r.down.avg, 3)}</div></div>
          </div>
        </div>
      </Panel>

      <div className="grid gap-5 xl:grid-cols-[minmax(0,1.4fr)_minmax(0,1fr)]">
        <Panel>
          <PanelHeader icon={<Radar />} title="The six coins" />
          <div className="grid gap-3 p-4 sm:grid-cols-2 lg:grid-cols-3">
            {ASSETS.map((a) => {
              const d = deltas[a], pr = prices[a], m = ASSET_META[a];
              const dir = d == null ? "—" : d >= cfg.minSpotDeltaPct ? "UP" : d <= -cfg.minSpotDeltaPct ? "DOWN" : "FLAT";
              return (
                <div key={a} className="rounded-2xl border border-line bg-surf-1/50 p-4">
                  <div className="flex items-center justify-between"><span className="flex items-center gap-2 font-num font-bold text-tx-hi"><span style={{ color: m.color }}>{m.glyph}</span>{m.sym}</span>
                    <Badge className={cn(dir === "UP" ? "border-up/50 text-up" : dir === "DOWN" ? "border-down/50 text-down" : "border-line-hi text-tx-low")}>{dir}</Badge></div>
                  <div className={cn("num mt-2 text-2xl font-semibold", tone(d))}>{fmtPct(d, 3)}</div>
                  <div className="num mt-1 text-[11px] text-tx-low">open ${fmtPrice(spot[a].windowOpen)} → ${fmtPrice(spot[a].price)}</div>
                  <div className="mt-3 grid grid-cols-2 gap-2 text-center">
                    <div className="rounded-lg bg-up/10 py-1.5"><div className="text-[9.5px] text-up/80">UP ask</div><div className="num text-sm font-semibold text-up">{fmtCents(pr.up)}</div></div>
                    <div className="rounded-lg bg-down/10 py-1.5"><div className="text-[9.5px] text-down/80">DOWN ask</div><div className="num text-sm font-semibold text-down">{fmtCents(pr.down)}</div></div>
                  </div>
                </div>
              );
            })}
          </div>
        </Panel>
        <Panel>
          <PanelHeader icon={<Crosshair />} title="Who could we buy?" />
          {r.candidates.length === 0 ? <Empty>No laggard candidates: either fewer than {cfg.minConsensus} coins agree, or every coin is moving with the group.</Empty> : (
            <div className="space-y-3 p-4">
              {r.candidates.map((c) => (
                <div key={c.asset} className={cn("rounded-2xl border p-4", c.pass ? "border-up/60 bg-up/10" : "border-line bg-surf-1/50")}>
                  <div className="flex items-center justify-between">
                    <span className="font-num font-bold text-tx-hi">{ASSET_META[c.asset].sym} · buy {c.side} @ {fmtCents(c.price)}</span>
                    <Badge className={c.pass ? "border-up/60 text-up" : "border-line-hi text-tx-low"}>{c.pass ? "all gates pass" : "blocked"}</Badge>
                  </div>
                  <div className="mt-2.5 flex flex-wrap gap-2">
                    {Object.entries(c.gates).map(([g, ok]) => <Badge key={g} className={ok ? "border-up/40 text-up" : "border-down/40 text-down"}>{ok ? "✓" : "✕"} {g}</Badge>)}
                  </div>
                  <div className="num mt-2 text-[11px] text-tx-low">laggard move {fmtPct(c.delta, 3)}</div>
                </div>
              ))}
            </div>
          )}
          <p className="border-t border-line px-4 py-3 text-[11px] leading-relaxed text-tx-dim">
            Live preview of the gates, computed in your browser. The engine's own decisions, including every skip and its reason, are listed below and scored when each round settles.
          </p>
        </Panel>
      </div>

      <Panel>
        <PanelHeader icon={<History />} title="Recent signals (all strategies)"
          right={<Badge className={live ? "border-up/50 text-up" : "border-line-hi text-tx-low"}>{live ? "live" : "connecting"}</Badge>} />
        <SignalTable />
      </Panel>
    </div>
  );
}
