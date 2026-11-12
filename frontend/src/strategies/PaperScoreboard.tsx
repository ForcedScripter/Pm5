import { useQuery } from "@tanstack/react-query";
import { Gauge } from "lucide-react";
import { Fragment, useState } from "react";
import { Empty, Panel, PanelHeader } from "@/components/ui/panel";
import { cn, fmtUsd, tone } from "@/lib/utils";
import { Api } from "@/services/api";
import type { ScoreVerdict } from "@/types/api";

const pct = (v: number | null | undefined) => (v == null ? "—" : `${(v * 100).toFixed(1)}%`);
const usd = (v: number | null | undefined) => (v == null ? "—" : fmtUsd(v, true));
const VERDICT: Record<ScoreVerdict, string> = {
  passing: "border-up/60 text-up",
  losing: "border-down/60 text-down",
  inconclusive: "border-amber/50 text-amber-hi",
  "not enough data": "border-line-hi text-tx-low",
};

/** One fixed test for every strategy: enough trades, the 95% interval on P&L per trade above zero,
 *  and no single day carrying the result. Shown before anything gets real money. */
export function PaperScoreboard({ mode }: { mode: string }) {
  const q = useQuery({ queryKey: ["scoreboard", mode], queryFn: () => Api.scoreboard(mode), refetchInterval: 30_000 });
  const [open, setOpen] = useState<string | null>(null);
  const rows = q.data?.strategies ?? [];
  const rule = q.data?.rule;
  const cal = q.data?.calibration ?? [];
  return (
    <Panel>
      <PanelHeader icon={<Gauge />} title="Scoreboard: does each strategy have an edge?" />
      {rule && (
        <p className="border-b border-line px-4 py-2 text-[11.5px] leading-relaxed text-tx-low">
          A strategy passes with at least {rule.min_trades} closed trades ({rule.min_rounds} rounds for the maker), a 95% interval on
          P&amp;L per trade that sits entirely above $0, and no single day supplying more than {Math.round(rule.max_day_share * 100)}% of the
          profit. It is marked losing once {rule.min_for_verdict}+ trades put the whole interval below $0. Click a row for price bands.
        </p>
      )}
      {rows.length === 0 ? <Empty>{q.isLoading ? "Loading…" : "No closed trades yet. Results appear here as rounds settle."}</Empty> : (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[860px] text-[12.5px]">
            <thead><tr className="border-b border-line text-left font-num text-[10.5px] tracking-wider text-tx-low uppercase">
              {["Strategy", "Trades", "Win rate", "Needed to break even", "P&L", "Per trade", "95% interval", "Biggest day", "Verdict"].map((h) => <th key={h} className="px-3 py-2">{h}</th>)}
            </tr></thead>
            <tbody>{rows.map((s) => (
              <Fragment key={s.strategy}>
                <tr className="cursor-pointer border-b border-line/60 hover:bg-bg-0/40" onClick={() => setOpen(open === s.strategy ? null : s.strategy)}>
                  <td className="px-3 py-2 font-semibold text-tx-hi">{s.name}</td>
                  <td className="num px-3 py-2">{s.n}<span className="text-tx-low"> / {s.need}</span></td>
                  <td className="num px-3 py-2">{pct(s.win_rate)}</td>
                  <td className="num px-3 py-2" title="Average win rate needed after the taker fee, at the prices actually paid (positions held to settlement)">{pct(s.breakeven_win_rate)}</td>
                  <td className={cn("num px-3 py-2", tone(s.pnl))}>{usd(s.pnl)}</td>
                  <td className={cn("num px-3 py-2 font-semibold", tone(s.pnl_per_trade))}>{usd(s.pnl_per_trade)}</td>
                  <td className="num px-3 py-2 text-tx-dim">{s.ci_low == null ? "—" : `${usd(s.ci_low)} to ${usd(s.ci_high)}`}</td>
                  <td className="num px-3 py-2 text-tx-dim" title="Largest single day's share of total profit">{pct(s.max_day_share)}</td>
                  <td className="px-3 py-2"><span className={cn("rounded-full border px-2 py-0.5 font-num text-[10px] font-bold uppercase", VERDICT[s.verdict])}>{s.verdict}</span></td>
                </tr>
                {open === s.strategy && s.strict && (
                  <tr className="border-b border-line/40 bg-bg-0/30 text-tx-dim">
                    <td className="px-3 py-1.5 pl-8" title="Twice the queue, and a trade must go a full tick through our price">strict fill model</td>
                    <td className="num px-3 py-1.5">{s.strict.n}</td><td /><td />
                    <td className={cn("num px-3 py-1.5", tone(s.strict.pnl))}>{usd(s.strict.pnl)}</td>
                    <td className={cn("num px-3 py-1.5", tone(s.strict.pnl_per_trade))}>{usd(s.strict.pnl_per_trade)}</td>
                    <td className="num px-3 py-1.5">{s.strict.ci_low == null ? "—" : `${usd(s.strict.ci_low)} to ${usd(s.strict.ci_high)}`}</td>
                    <td />
                    <td className="px-3 py-1.5 font-num text-[10px] uppercase">{s.strict.verdict}</td>
                  </tr>
                )}
                {open === s.strategy && s.bands.map((b) => (
                  <tr key={b.band} className="border-b border-line/40 bg-bg-0/30 text-tx-dim">
                    <td className="px-3 py-1.5 pl-8">entry {b.band}</td>
                    <td className="num px-3 py-1.5">{b.n}</td>
                    <td className="num px-3 py-1.5">{pct(b.win_rate)}</td>
                    <td />
                    <td className={cn("num px-3 py-1.5", tone(b.pnl))}>{usd(b.pnl)}</td>
                    <td className={cn("num px-3 py-1.5", tone(b.pnl_per_trade))}>{usd(b.pnl_per_trade)}</td>
                    <td className="num px-3 py-1.5">{b.ci_low == null ? "—" : `${usd(b.ci_low)} to ${usd(b.ci_high)}`}</td>
                    <td />
                    <td className="px-3 py-1.5 font-num text-[10px] uppercase">{b.verdict}</td>
                  </tr>
                ))}
              </Fragment>
            ))}</tbody>
          </table>
        </div>
      )}
      {cal.length > 0 && (
        <div className="border-t border-line px-4 py-3">
          <p className="mb-2 text-[11.5px] text-tx-low">
            Fair-value model check: of the signals the model rated in each range, how many actually won. The two columns should match.
          </p>
          <table className="text-[12.5px]">
            <thead><tr className="text-left font-num text-[10.5px] tracking-wider text-tx-low uppercase">
              {["Model said", "Signals", "Model average", "Actually won"].map((h) => <th key={h} className="py-1 pr-6">{h}</th>)}
            </tr></thead>
            <tbody>{cal.map((c) => (
              <tr key={c.bucket}>
                <td className="num py-1 pr-6">{c.bucket}</td><td className="num py-1 pr-6">{c.n}</td>
                <td className="num py-1 pr-6">{pct(c.mean_fair)}</td>
                <td className={cn("num py-1 pr-6 font-semibold", c.n >= 30 ? tone(c.win_rate - c.mean_fair) : "text-tx-low")}>{pct(c.win_rate)}</td>
              </tr>))}</tbody>
          </table>
        </div>
      )}
    </Panel>
  );
}
