import { useQuery } from "@tanstack/react-query";
import { Target } from "lucide-react";
import { Empty, Panel, PanelHeader } from "@/components/ui/panel";
import { cn, fmtUsd, tone } from "@/lib/utils";
import { Api } from "@/services/api";
import type { StrikeMarketRow } from "@/types/api";

const px = (v: number | null | undefined) => (v == null ? "—" : v.toFixed(3));
const KIND: Record<string, string> = { above: "above at noon ET", reach: "reaches", dip: "dips to" };
const when = (ts: number) => new Date(ts * 1000).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });

/** Crypto strike markets on paper. The two checks come first: is the model better than the market's
 *  own prices, and does our Binance settlement agree with Polymarket's. P&L means little until both hold. */
export function StrikesPanel({ mode }: { mode: string }) {
  const stats = useQuery({ queryKey: ["strike-stats", mode], queryFn: () => Api.strikeStats(mode), refetchInterval: 30_000 });
  const pos = useQuery({ queryKey: ["strike-positions", mode], queryFn: () => Api.strikePositions(mode, 40), refetchInterval: 20_000 });
  const mk = useQuery({ queryKey: ["strike-markets"], queryFn: () => Api.strikeMarkets(), refetchInterval: 20_000 });
  const s = stats.data;
  const rows = pos.data ?? [];
  const near = (mk.data?.markets ?? []).filter((m: StrikeMarketRow) => m.model != null && m.model > 0.03 && m.model < 0.97);
  const status = mk.data?.status;
  return (
    <Panel>
      <PanelHeader icon={<Target />} title="Crypto strikes (BTC / ETH above, reach, dip)" />
      {s?.disclaimer && <p className="border-b border-line px-4 py-2 text-[11.5px] leading-relaxed text-tx-low">{s.disclaimer}</p>}
      {s && (
        <div className="grid gap-4 border-b border-line px-4 py-3 text-[12.5px] md:grid-cols-3">
          <div>
            <div className="font-num text-[10.5px] tracking-wider text-tx-low uppercase">Paper positions</div>
            <div className="num mt-1">{s.positions.open} open (${s.positions.open_cost.toFixed(2)}) · {s.positions.closed} closed, {s.positions.wins} won</div>
            <div className={cn("num font-semibold", tone(s.positions.pnl))}>{fmtUsd(s.positions.pnl, true)}</div>
          </div>
          <div>
            <div className="font-num text-[10.5px] tracking-wider text-tx-low uppercase">Our Binance settlement vs Polymarket</div>
            <div className="num mt-1">{s.settlement.checked === 0 ? "no resolved markets checked yet" : `${s.settlement.agree} of ${s.settlement.checked} agree`}</div>
            {s.settlement.disagree > 0 && <div className="text-down">{s.settlement.disagree} disagree: do not trust the model's settlement rule yet</div>}
          </div>
          <div>
            <div className="font-num text-[10.5px] tracking-wider text-tx-low uppercase">Engine</div>
            <div className="num mt-1">{status ? `${status.markets} markets · ${status.books} books · ${status.quotes} resting bids` : "not running"}</div>
            {status?.last_error && <div className="truncate text-tx-low" title={status.last_error}>last error: {status.last_error}</div>}
          </div>
        </div>
      )}
      {s && s.calibration.length > 0 && (
        <div className="border-b border-line px-4 py-3">
          <p className="mb-2 text-[11.5px] text-tx-low">
            Model against market on every resolved market, traded or not. Brier score: lower is better. The model only has an edge where its score is below the market's.
          </p>
          <table className="text-[12.5px]">
            <thead><tr className="text-left font-num text-[10.5px] tracking-wider text-tx-low uppercase">
              {["Market type", "Snapshot", "Markets", "Model", "Market price"].map((h) => <th key={h} className="py-1 pr-6">{h}</th>)}
            </tr></thead>
            <tbody>{s.calibration.map((c) => (
              <tr key={`${c.kind}-${c.horizon}`}>
                <td className="py-1 pr-6">{c.kind}</td><td className="py-1 pr-6">{c.horizon}</td><td className="num py-1 pr-6">{c.n}</td>
                <td className={cn("num py-1 pr-6 font-semibold", c.n >= 30 && c.model_brier != null && c.market_brier != null ? tone(c.market_brier - c.model_brier) : "text-tx-low")}>{c.model_brier?.toFixed(4) ?? "—"}</td>
                <td className="num py-1 pr-6">{c.market_brier?.toFixed(4) ?? "—"}</td>
              </tr>))}</tbody>
          </table>
        </div>
      )}
      {near.length > 0 && (
        <div className="overflow-x-auto border-b border-line">
          <table className="w-full min-w-[820px] text-[12.5px]">
            <thead><tr className="border-b border-line text-left font-num text-[10.5px] tracking-wider text-tx-low uppercase">
              {["Asset", "Question", "Expires", "Model", "Bid", "Ask", "Model − mid", "Vol used", "Our resting bid"].map((h) => <th key={h} className="px-3 py-2">{h}</th>)}
            </tr></thead>
            <tbody>{near.map((m) => {
              const mid = m.bid != null && m.ask != null ? (m.bid + m.ask) / 2 : null;
              const gap = mid != null && m.model != null ? m.model - mid : null;
              const rest = Object.entries(m.resting).map(([side, q]) => `${side} ${q.shares} @ ${q.price.toFixed(3)}`).join(", ");
              return (
                <tr key={m.id} className="border-b border-line/60">
                  <td className="px-3 py-1.5 font-num font-bold text-tx-hi">{m.asset.toUpperCase()}</td>
                  <td className="px-3 py-1.5">{KIND[m.kind] ?? m.kind} ${m.strike.toLocaleString()}</td>
                  <td className="num px-3 py-1.5 whitespace-nowrap text-tx-dim">{when(m.expiry)}</td>
                  <td className="num px-3 py-1.5 font-semibold">{px(m.model)}</td>
                  <td className="num px-3 py-1.5">{px(m.bid)}</td><td className="num px-3 py-1.5">{px(m.ask)}</td>
                  <td className={cn("num px-3 py-1.5", gap == null ? "" : tone(gap))}>{gap == null ? "—" : `${gap >= 0 ? "+" : ""}${(gap * 100).toFixed(1)}¢`}</td>
                  <td className="num px-3 py-1.5 text-tx-low">{m.vol == null ? "—" : `${(m.vol * 100).toFixed(0)}%`}</td>
                  <td className="num px-3 py-1.5 text-amber-hi">{rest || "—"}</td>
                </tr>);
            })}</tbody>
          </table>
        </div>
      )}
      {rows.length === 0 ? <Empty>{pos.isLoading ? "Loading…" : "No strike positions yet. Enable Crypto Strikes in Settings and start the bot; a resting bid fills only when the market trades through it."}</Empty> : (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[820px] text-[12.5px]">
            <thead><tr className="border-b border-line text-left font-num text-[10.5px] tracking-wider text-tx-low uppercase">
              {["Opened", "Asset", "Question", "Side", "Shares", "Entry", "Model at entry", "Mark / exit", "Status", "P&L"].map((h) => <th key={h} className="px-3 py-2">{h}</th>)}
            </tr></thead>
            <tbody>{rows.map((r) => (
              <tr key={r.id} className="border-b border-line/60">
                <td className="num px-3 py-1.5 whitespace-nowrap text-tx-dim">{new Date(r.opened_at).toLocaleString()}</td>
                <td className="px-3 py-1.5 font-num font-bold text-tx-hi">{r.asset.toUpperCase()}</td>
                <td className="px-3 py-1.5">{KIND[r.kind] ?? r.kind} ${r.strike.toLocaleString()}</td>
                <td className={cn("px-3 py-1.5 font-num font-bold", r.side === "YES" ? "text-up" : "text-down")}>{r.side}</td>
                <td className="num px-3 py-1.5">{r.shares}</td>
                <td className="num px-3 py-1.5">{px(r.entry_price)}</td>
                <td className="num px-3 py-1.5">{px(r.model_prob)}</td>
                <td className="num px-3 py-1.5">{px(r.status === "open" ? r.mark : r.exit_price)}</td>
                <td className="px-3 py-1.5">{r.status}</td>
                <td className={cn("num px-3 py-1.5 font-semibold", tone(r.pnl ?? 0))}>{r.pnl == null ? "—" : fmtUsd(r.pnl, true)}</td>
              </tr>))}</tbody>
          </table>
        </div>
      )}
    </Panel>
  );
}
