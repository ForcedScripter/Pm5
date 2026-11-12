import { useQuery } from "@tanstack/react-query";
import { Layers } from "lucide-react";
import { Empty, Panel, PanelHeader } from "@/components/ui/panel";
import { cn, fmtUsd, tone } from "@/lib/utils";
import { Api } from "@/services/api";

const pct = (v: number | null) => (v == null ? "—" : `${(v * 100).toFixed(0)}%`);
const cents = (v: number | null) => (v == null ? "—" : `${v >= 0 ? "+" : ""}${(v * 100).toFixed(1)}¢`);

/** Spork Maker paper rounds. The diagnostics come first on purpose: one-sided share and markouts
 *  show adverse selection, which decides whether the P&L means anything. */
export function MakerRounds({ mode }: { mode: string }) {
  const stats = useQuery({ queryKey: ["maker-stats", mode], queryFn: () => Api.makerStats(mode), refetchInterval: 15_000 });
  const rounds = useQuery({ queryKey: ["maker-rounds", mode], queryFn: () => Api.makerRounds(mode, 30), refetchInterval: 15_000 });
  const rs = rounds.data ?? [];
  const st = stats.data?.stats ?? [];
  return (
    <Panel>
      <PanelHeader icon={<Layers />} title="Maker rounds (Spork Maker)" />
      {stats.data?.disclaimer && <p className="border-b border-line px-4 py-2 text-[11.5px] leading-relaxed text-tx-low">{stats.data.disclaimer}</p>}
      {st.length > 0 && (
        <div className="overflow-x-auto border-b border-line">
          <table className="w-full min-w-[760px] text-[12.5px]">
            <thead><tr className="border-b border-line text-left font-num text-[10.5px] tracking-wider text-tx-low uppercase">
              {["Asset", "Rounds", "P&L", "Strict P&L", "Per round", "Won", "One-sided", "Fills", "Markout 5s", "Markout 30s", "Reward time", "Rebate (est.)"].map((h) => <th key={h} className="px-3 py-2">{h}</th>)}
            </tr></thead>
            <tbody>{st.map((s) => (
              <tr key={s.asset} className={cn("border-b border-line/60", s.asset === "all" && "font-semibold")}>
                <td className="px-3 py-2 font-num font-bold text-tx-hi">{s.asset.toUpperCase()}</td>
                <td className="num px-3 py-2">{s.rounds}</td>
                <td className={cn("num px-3 py-2", tone(s.pnl))}>{fmtUsd(s.pnl, true)}</td>
                <td className={cn("num px-3 py-2", tone(s.pnl_strict ?? 0))} title="Same quotes under the harsher fill model: twice the queue, and a trade must go a full tick through our price">{s.pnl_strict == null ? "—" : fmtUsd(s.pnl_strict, true)}</td>
                <td className={cn("num px-3 py-2", tone(s.pnl_per_round ?? 0))}>{s.pnl_per_round == null ? "—" : fmtUsd(s.pnl_per_round, true)}</td>
                <td className="num px-3 py-2">{s.win_rounds}/{s.rounds}</td>
                <td className="num px-3 py-2" title="Rounds where (almost) only one side sold: the trend / adverse-selection case">{pct(s.one_sided_share)}</td>
                <td className="num px-3 py-2">{s.fills}</td>
                <td className={cn("num px-3 py-2", tone(s.markout_5s ?? 0))} title="Sale price minus that side's fair value 5s later (+ = sold above where it went)">{cents(s.markout_5s)}</td>
                <td className={cn("num px-3 py-2", tone(s.markout_30s ?? 0))}>{cents(s.markout_30s)}</td>
                <td className="num px-3 py-2 text-tx-low" title="Time both quotes were big and close enough to earn liquidity rewards">{s.reward_s ? `${Math.round(s.reward_s / 60)} min` : "—"}</td>
                <td className="num px-3 py-2 text-tx-low" title="20% of the taker fees on our fills. Reported only; not in the P&L">{fmtUsd(s.rebate_est)}</td>
              </tr>))}</tbody>
          </table>
        </div>
      )}
      {rs.length === 0 ? <Empty>{rounds.isLoading ? "Loading…" : "No maker rounds yet. Enable Spork Maker and start the bot; a round is minted at each eligible market's open."}</Empty> : (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[900px] text-[12.5px]">
            <thead><tr className="border-b border-line text-left font-num text-[10.5px] tracking-wider text-tx-low uppercase">
              {["Round end", "Asset", "Status", "Sets", "Sold ▲/▼", "Proceeds", "Leftover", "Winner", "Fills", "Edge", "P&L", "Strict"].map((h) => <th key={h} className="px-3 py-2">{h}</th>)}
            </tr></thead>
            <tbody>{rs.map((r) => (
              <tr key={r.id} className="border-b border-line/60">
                <td className="num px-3 py-2 whitespace-nowrap text-tx-dim">{new Date(r.window_end).toLocaleTimeString()}</td>
                <td className="px-3 py-2 font-num font-bold text-tx-hi">{r.asset.toUpperCase()}</td>
                <td className="px-3 py-2">{r.status}{r.one_sided ? <span className="ml-1 text-down" title="only one side sold">·1-sided</span> : null}</td>
                <td className="num px-3 py-2">{r.sets}{r.reward_sized ? <span className="ml-1 text-amber-hi" title="quoted at the liquidity-reward minimum size">★</span> : null}</td>
                <td className="num px-3 py-2"><span className="text-up">{r.sold_up}</span> / <span className="text-down">{r.sold_down}</span></td>
                <td className="num px-3 py-2">{fmtUsd(r.proceeds)}</td>
                <td className="num px-3 py-2">{r.residual_shares ? `${r.residual_shares} ${r.residual_side}` : "—"}</td>
                <td className={cn("px-3 py-2 font-num font-bold", r.winner === "UP" ? "text-up" : r.winner === "DOWN" ? "text-down" : "text-tx-low")}>{r.winner ?? "—"}</td>
                <td className="num px-3 py-2">{r.fills}</td>
                <td className="num px-3 py-2">{(r.edge * 100).toFixed(1)}¢</td>
                <td className={cn("num px-3 py-2 font-semibold", tone(r.pnl ?? 0))}>{r.pnl == null ? "—" : fmtUsd(r.pnl, true)}</td>
                <td className={cn("num px-3 py-2", tone(r.pnl_strict ?? 0))}>{r.pnl_strict == null ? "—" : fmtUsd(r.pnl_strict, true)}</td>
              </tr>))}</tbody>
          </table>
        </div>
      )}
    </Panel>
  );
}
