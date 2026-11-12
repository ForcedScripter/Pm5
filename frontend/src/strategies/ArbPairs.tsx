import { useQuery } from "@tanstack/react-query";
import { Scale } from "lucide-react";
import { Empty, Panel, PanelHeader } from "@/components/ui/panel";
import { cn, fmtUsd, tone } from "@/lib/utils";
import { Api } from "@/services/api";

/** Pair Arbitrage on paper. How often the gap appears matters as much as the P&L, so both are shown. */
export function ArbPairs({ mode }: { mode: string }) {
  const stats = useQuery({ queryKey: ["arb-stats", mode], queryFn: () => Api.arbStats(mode), refetchInterval: 20_000 });
  const pairs = useQuery({ queryKey: ["arb-pairs", mode], queryFn: () => Api.arbPairs(mode, 30), refetchInterval: 20_000 });
  const s = stats.data;
  const rows = pairs.data ?? [];
  const e = s?.engine;
  return (
    <Panel>
      <PanelHeader icon={<Scale />} title="Pair arbitrage (Up + Down for under $1)" />
      {s?.note && <p className="border-b border-line px-4 py-2 text-[11.5px] leading-relaxed text-tx-low">{s.note}</p>}
      {s && (
        <div className="grid gap-4 border-b border-line px-4 py-3 text-[12.5px] md:grid-cols-3">
          <div>
            <div className="font-num text-[10.5px] tracking-wider text-tx-low uppercase">Pairs bought</div>
            <div className="num mt-1">{s.pairs} ({s.open} open) · ${s.staked.toFixed(2)} staked</div>
            <div className={cn("num font-semibold", tone(s.pnl))}>{fmtUsd(s.pnl, true)} settled{s.locked_open ? ` · ${fmtUsd(s.locked_open, true)} locked in open pairs` : ""}</div>
          </div>
          <div>
            <div className="font-num text-[10.5px] tracking-wider text-tx-low uppercase">Since the server started</div>
            <div className="num mt-1">{e ? `${e.opportunities} gaps seen · ${e.missed} gone before an order could land · ${e.pairs} bought` : "engine not running"}</div>
          </div>
          <div>
            <div className="font-num text-[10.5px] tracking-wider text-tx-low uppercase">Cheapest pair seen</div>
            <div className="num mt-1" title="Up ask + Down ask + both taker fees; under $1.000 is an arbitrage">{e?.best_cost == null ? "—" : `$${e.best_cost.toFixed(4)}`}</div>
            {e?.last_block && <div className="truncate text-tx-low" title={e.last_block}>last block: {e.last_block}</div>}
          </div>
        </div>
      )}
      {rows.length === 0 ? <Empty>{pairs.isLoading ? "Loading…" : "No pairs, as expected: Up and Down are one mirrored book, so a real gap under $1 does not occur."}</Empty> : (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[760px] text-[12.5px]">
            <thead><tr className="border-b border-line text-left font-num text-[10.5px] tracking-wider text-tx-low uppercase">
              {["Bought", "Asset", "Pairs", "Up", "Down", "Fees", "All-in cost", "Pays", "Locked", "Status"].map((h) => <th key={h} className="px-3 py-2">{h}</th>)}
            </tr></thead>
            <tbody>{rows.map((r) => (
              <tr key={r.id} className="border-b border-line/60">
                <td className="num px-3 py-1.5 whitespace-nowrap text-tx-dim">{new Date(r.opened_at).toLocaleString()}</td>
                <td className="px-3 py-1.5 font-num font-bold text-tx-hi">{r.asset.toUpperCase()}</td>
                <td className="num px-3 py-1.5">{r.shares}</td>
                <td className="num px-3 py-1.5">{r.up_price.toFixed(3)}</td>
                <td className="num px-3 py-1.5">{r.down_price.toFixed(3)}</td>
                <td className="num px-3 py-1.5">{fmtUsd(r.fees)}</td>
                <td className="num px-3 py-1.5">{fmtUsd(r.cost + r.fees)}</td>
                <td className="num px-3 py-1.5">{fmtUsd(r.shares)}</td>
                <td className={cn("num px-3 py-1.5 font-semibold", tone(r.locked_pnl))}>{fmtUsd(r.locked_pnl, true)}</td>
                <td className="px-3 py-1.5">{r.status}</td>
              </tr>))}</tbody>
          </table>
        </div>
      )}
    </Panel>
  );
}
