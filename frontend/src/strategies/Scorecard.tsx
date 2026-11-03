import { useQuery } from "@tanstack/react-query";
import { cn } from "@/lib/utils";
import { Api } from "@/services/api";
import type { StrategyKey } from "@/types/api";

const SKIP_LABEL: Record<string, string> = {
  band: "price outside band", avoid_band: "in avoid band", spread: "spread too wide", depth: "thin book", book_stale: "stale book",
  no_book: "no book", too_early: "too early", too_late: "too late", skip_hour: "skipped hour", direction: "direction off",
  consensus_avg: "group avg too small", low_edge: "edge too small", low_confidence: "low confidence", cooldown: "cooldown",
  volatility: "volatility out of range", rsi: "RSI not confirming",
};

/** Measured paper performance of a strategy's calls, scored against how each round actually settled. */
export function Scorecard({ strategy }: { strategy: StrategyKey }) {
  const q = useQuery({ queryKey: ["signal-stats"], queryFn: Api.signalStats, refetchInterval: 30_000 });
  const s = q.data?.find((x) => x.strategy === strategy);
  if (!s) return null;
  const edge = s.hit_rate != null && s.breakeven_hit_rate != null ? s.hit_rate - s.breakeven_hit_rate : null;
  const cell = (label: string, value: React.ReactNode, tone?: string, sub?: string) => (
    <div className="min-w-0 rounded-2xl border border-line bg-bg-0/40 px-4 py-3">
      <div className="panel-title !text-[10px]">{label}</div>
      <div className={cn("num mt-1 truncate text-lg font-semibold text-tx-hi", tone)}>{value}</div>
      {sub && <div className="truncate text-[10.5px] text-tx-low">{sub}</div>}
    </div>
  );
  return (
    <div className="space-y-3">
      <div className="grid grid-cols-2 gap-2.5 md:grid-cols-5">
        {cell("Calls taken", s.accepted, undefined, `${s.resolved} settled`)}
        {cell("Hit rate", s.hit_rate == null ? "—" : `${(s.hit_rate * 100).toFixed(1)}%`, undefined, `${s.wins}W / ${s.resolved - s.wins}L`)}
        {cell("Break-even", s.breakeven_hit_rate == null ? "—" : `${(s.breakeven_hit_rate * 100).toFixed(1)}%`, undefined, "= avg price paid")}
        {cell("Edge", edge == null ? "—" : `${edge >= 0 ? "+" : "−"}${Math.abs(edge * 100).toFixed(1)} pts`, edge == null ? "" : edge >= 0 ? "text-up" : "text-down",
          s.resolved < 100 ? "too few to trust yet" : "hit rate − break-even")}
        {cell("P&L per $1", s.avg_pnl_per_dollar == null ? "—" : `${s.avg_pnl_per_dollar >= 0 ? "+" : "−"}$${Math.abs(s.avg_pnl_per_dollar).toFixed(3)}`,
          (s.avg_pnl_per_dollar ?? 0) >= 0 ? "text-up" : "text-down", `total ${s.total_pnl_per_dollar >= 0 ? "+" : "−"}$${Math.abs(s.total_pnl_per_dollar).toFixed(2)} on $1/call`)}
      </div>
      {s.skips.length > 0 && (
        <div className="flex flex-wrap gap-2 text-[11.5px]">
          <span className="font-num tracking-wider text-tx-low uppercase">Top skips</span>
          {s.skips.slice(0, 6).map((k) => (
            <span key={k.code} className="rounded-full border border-line-mid px-2.5 py-0.5 text-tx" title={k.would_have_won != null ? `of ${k.resolved} settled, ${(k.would_have_won * 100).toFixed(0)}% would have won` : ""}>
              {SKIP_LABEL[k.code] ?? k.code} · {k.n}{k.would_have_won != null && <span className="text-tx-dim"> · {(k.would_have_won * 100).toFixed(0)}% would win</span>}
            </span>
          ))}
        </div>
      )}
      <p className="text-[11px] text-tx-dim">Paper results at the ask seen when the signal fired, $1 per call, before fees. A binary bought at p only breaks even if it wins p of the time.</p>
    </div>
  );
}
