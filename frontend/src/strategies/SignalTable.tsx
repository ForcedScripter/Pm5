import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Empty } from "@/components/ui/panel";
import { cn, fmtCents } from "@/lib/utils";
import { Api } from "@/services/api";
import type { SignalRow } from "@/types/api";

const STRAT_LABEL: Record<string, string> = {
  constellation: "Constellation", ai_strat: "AI Strat", mean_reversion: "Mean Rev", custom_btc: "Custom BTC",
  custom_eth: "Custom ETH", pocket_aces: "Pocket Aces",
};

function Result({ s }: { s: SignalRow }) {
  if (s.outcome == null) return <span className="text-tx-dim">pending</span>;
  if (s.status !== "accepted") return <span className={s.won ? "text-up/70" : "text-down/70"}>{s.won ? "would have won" : "would have lost"}</span>;
  return <span className={cn("num font-semibold", s.won ? "text-up" : "text-down")}>
    {s.won ? "WIN" : "LOSS"} {s.pnl_per_dollar != null ? `${s.pnl_per_dollar >= 0 ? "+" : "−"}${Math.abs(s.pnl_per_dollar).toFixed(2)}/$` : ""}
  </span>;
}

export function SignalTable({ strategy, compact }: { strategy?: string; compact?: boolean }) {
  const [status, setStatus] = useState<"" | "accepted" | "rejected">("");
  const q = useQuery({ queryKey: ["signals", strategy ?? "all", status], queryFn: () => Api.signals({ strategy, status, limit: compact ? 15 : 100 }),
                       refetchInterval: 15_000 });
  const rows = q.data ?? [];
  return (
    <div>
      <div className="flex gap-1 px-4 py-2" role="tablist">
        {([["", "All"], ["accepted", "Taken"], ["rejected", "Skipped"]] as const).map(([v, l]) => (
          <button key={v} role="tab" aria-selected={status === v} onClick={() => setStatus(v)}
            className={cn("rounded-full px-3 py-1 font-num text-[11px] font-bold tracking-wider uppercase", status === v ? "gold-pill" : "text-tx-mid hover:text-tx-hi")}>{l}</button>
        ))}
      </div>
      {q.error ? <Empty>{q.error.message}</Empty> : rows.length === 0 ? (
        <Empty>{q.isLoading ? "Loading…" : "No signals yet. Start the bot with at least one strategy enabled; every call and every skip (with its reason) lands here and is scored when the round settles."}</Empty>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[720px] text-[12.5px]">
            <thead><tr className="border-b border-line text-left font-num text-[10.5px] tracking-wider text-tx-low uppercase">
              <th className="px-4 py-2">Time</th>{!strategy && <th className="px-2">Strategy</th>}<th className="px-2">Call</th>
              <th className="px-2 text-right">Price</th><th className="px-2">Status</th><th className="px-2">Why</th><th className="px-4 text-right">Result</th>
            </tr></thead>
            <tbody>
              {rows.map((s) => (
                <tr key={s.id} className="border-b border-line/60 align-top">
                  <td className="num px-4 py-2 whitespace-nowrap text-tx-dim">{new Date(s.ts).toLocaleTimeString()}<div className="text-[10.5px]">{s.secs_in}s in</div></td>
                  {!strategy && <td className="px-2 py-2 whitespace-nowrap text-tx">{STRAT_LABEL[s.strategy] ?? s.strategy}</td>}
                  <td className="px-2 py-2 whitespace-nowrap font-num font-bold"><span className="text-tx-hi">{s.asset.toUpperCase()}</span>{" "}
                    <span className={s.side === "UP" ? "text-up" : "text-down"}>{s.side}</span></td>
                  <td className="num px-2 py-2 text-right">{s.price > 0 ? fmtCents(s.price) : "—"}</td>
                  <td className="px-2 py-2"><span className={cn("rounded-full border px-2 py-0.5 font-num text-[10px] font-bold uppercase",
                    s.status === "accepted" ? "border-up/50 text-up" : "border-line-hi text-tx-low")}>{s.status === "accepted" ? "taken" : s.code ?? "skip"}</span></td>
                  <td className="px-2 py-2 text-tx">{s.reason}</td>
                  <td className="px-4 py-2 text-right whitespace-nowrap"><Result s={s} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
