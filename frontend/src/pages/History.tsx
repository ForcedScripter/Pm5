import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { History as HistoryIcon, ListOrdered, RotateCcw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useMemo, useState } from "react";
import { Empty, Panel, PanelHeader, Stat } from "@/components/ui/panel";
import { usePortfolioStream } from "@/hooks/usePortfolioStream";
import { ASSETS, cn, fmtCents, fmtUsd, tone } from "@/lib/utils";
import { Api } from "@/services/api";
import { ArbPairs } from "@/strategies/ArbPairs";
import { MakerRounds } from "@/strategies/MakerRounds";
import { PaperScoreboard } from "@/strategies/PaperScoreboard";
import { StrikesPanel } from "@/strategies/StrikesPanel";
import { WeatherPanel } from "@/strategies/WeatherPanel";

const STRATS = ["binance_lead", "twap_lock", "constellation", "odds_scalper", "pocket_aces", "custom_btc"];
const sel = "h-9 rounded-full border border-line-mid bg-bg-0/70 px-3 text-[12px] text-tx-hi focus:border-amber/70 focus:outline-none";
const dur = (s: number) => (s < 60 ? `${s}s` : s < 3600 ? `${Math.round(s / 60)}m` : `${(s / 3600).toFixed(1)}h`);

export default function History() {
  usePortfolioStream();
  const qc = useQueryClient();
  const [confirmReset, setConfirmReset] = useState(false);
  const resetBook = useMutation({
    mutationFn: () => Api.resetBook(),
    onSuccess: () => {
      setConfirmReset(false);
      qc.invalidateQueries({ queryKey: ["portfolio"] });
      qc.invalidateQueries({ queryKey: ["positions"] });
      qc.invalidateQueries({ queryKey: ["orders"] });
      qc.invalidateQueries({ queryKey: ["trades"] });
      qc.invalidateQueries({ queryKey: ["logs"] });
    },
  });
  const pf = useQuery({ queryKey: ["portfolio"], queryFn: () => Api.portfolio() });
  const [mode, setMode] = useState<"paper" | "live" | "">("");
  const m = mode || pf.data?.mode || "paper";
  const [f, setF] = useState({ asset: "", strategy: "", result: "", from: "", to: "" });
  const trades = useQuery({
    queryKey: ["trades", m, f], queryFn: () => Api.trades({
      mode: m, ...f,
      from: f.from ? new Date(f.from).toISOString() : "", to: f.to ? new Date(f.to + "T23:59:59").toISOString() : ""
    }), refetchInterval: 15_000
  });
  const orders = useQuery({ queryKey: ["orders", m], queryFn: () => Api.orders(m), refetchInterval: 10_000 });
  const t = trades.data ?? [];
  const sum = useMemo(() => {
    const wins = t.filter((x) => x.pnl > 0), losses = t.filter((x) => x.pnl < 0);
    const gw = wins.reduce((a, x) => a + x.pnl, 0), gl = -losses.reduce((a, x) => a + x.pnl, 0);
    return {
      n: t.length, pnl: t.reduce((a, x) => a + x.pnl, 0), fees: t.reduce((a, x) => a + x.fees, 0),
      hit: t.length ? wins.length / t.length : null, pf: gl ? gw / gl : null
    };
  }, [t]);
  return (
    <div className="space-y-5">
      <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
        <Stat label="Trades (filtered)" value={sum.n} accent="#c6b28c" />
        <Stat label="Net P&L" value={fmtUsd(sum.pnl, true)} tone={tone(sum.pnl)} sub="after fees" accent="#35d49a" />
        <Stat label="Hit rate" value={sum.hit == null ? "—" : `${(sum.hit * 100).toFixed(1)}%`} accent="#f79240" />
        <Stat label="Profit factor" value={sum.pf == null ? "—" : sum.pf.toFixed(2)} sub="gross win ÷ gross loss" accent="#5cc8ff" />
        <Stat label="Fees paid" value={fmtUsd(sum.fees)} sub="Polymarket taker fees" accent="#ff5a76" />
      </div>
      <Panel>
        <PanelHeader
          icon={<HistoryIcon />}
          title="Trade history"
          right={m === "paper" ? (
            confirmReset ? (
              <div className="flex items-center gap-1.5">
                <span className="text-[11px] text-tx-mid">{resetBook.error ? <span className="text-down">{resetBook.error.message}</span> : "Reset book?"}</span>
                <Button size="sm" variant="danger" className="h-7 px-2.5 text-xs" loading={resetBook.isPending} onClick={() => resetBook.mutate()}>
                  Confirm
                </Button>
                <Button size="sm" variant="ghost" className="h-7 px-2 text-xs" onClick={() => setConfirmReset(false)}>
                  Cancel
                </Button>
              </div>
            ) : (
              <Button size="sm" variant="outline" className="h-7 px-2.5 text-xs hover:border-down/60 hover:text-down" onClick={() => setConfirmReset(true)}>
                <RotateCcw className="mr-1 h-3 w-3" />
                Reset paper book
              </Button>
            )
          ) : undefined}
        />
        <div className="flex flex-wrap gap-2 border-b border-line px-4 py-3">
          <select aria-label="mode" className={sel} value={m} onChange={(e) => setMode(e.target.value as "paper" | "live")}><option value="paper">Paper</option><option value="live">Live</option></select>
          <select aria-label="asset" className={sel} value={f.asset} onChange={(e) => setF({ ...f, asset: e.target.value })}><option value="">All assets</option>{ASSETS.map((a) => <option key={a} value={a}>{a.toUpperCase()}</option>)}</select>
          <select aria-label="strategy" className={sel} value={f.strategy} onChange={(e) => setF({ ...f, strategy: e.target.value })}><option value="">All strategies</option>{STRATS.map((s) => <option key={s} value={s}>{s}</option>)}</select>
          <select aria-label="outcome" className={sel} value={f.result} onChange={(e) => setF({ ...f, result: e.target.value })}><option value="">All outcomes</option><option value="won">Won</option><option value="lost">Lost</option><option value="stopped">Stopped</option><option value="sold">Sold</option></select>
          <input aria-label="from" type="date" className={sel} value={f.from} onChange={(e) => setF({ ...f, from: e.target.value })} />
          <input aria-label="to" type="date" className={sel} value={f.to} onChange={(e) => setF({ ...f, to: e.target.value })} />
        </div>
        {t.length === 0 ? <Empty>{trades.isLoading ? "Loading…" : "No closed trades match. Positions become trades when their round settles (or a stop-loss / take-profit closes them)."}</Empty> : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[900px] text-[12.5px]">
              <thead><tr className="border-b border-line text-left font-num text-[10.5px] tracking-wider text-tx-low uppercase">
                {["Closed", "Asset", "Strategy", "Side", "Entry", "Exit", "Shares", "Cost", "Fees", "P&L", "Held", "Result"].map((h) => <th key={h} className="px-3 py-2">{h}</th>)}
              </tr></thead>
              <tbody>{t.map((x) => (
                <tr key={x.id} className="border-b border-line/60">
                  <td className="num px-3 py-2 whitespace-nowrap text-tx-dim">{new Date(x.closed_at).toLocaleString()}</td>
                  <td className="px-3 py-2 font-num font-bold text-tx-hi">{x.asset.toUpperCase()}</td>
                  <td className="px-3 py-2 text-tx">{x.strategy}</td>
                  <td className={cn("px-3 py-2 font-num font-bold", x.side === "UP" ? "text-up" : "text-down")}>{x.side}</td>
                  <td className="num px-3 py-2">{fmtCents(x.entry_avg)}</td><td className="num px-3 py-2">{fmtCents(x.exit_price)}</td>
                  <td className="num px-3 py-2">{x.shares.toFixed(2)}</td><td className="num px-3 py-2">{fmtUsd(x.cost)}</td>
                  <td className="num px-3 py-2 text-tx-low">{fmtUsd(x.fees)}</td>
                  <td className={cn("num px-3 py-2 font-semibold", tone(x.pnl))}>{fmtUsd(x.pnl, true)}</td>
                  <td className="num px-3 py-2 text-tx-low">{dur(x.duration_s)}</td>
                  <td className="px-3 py-2"><span className={cn("rounded-full border px-2 py-0.5 font-num text-[10px] font-bold uppercase", x.pnl > 0 ? "border-up/50 text-up" : "border-down/50 text-down")}>{x.result}</span></td>
                </tr>))}</tbody>
            </table>
          </div>
        )}
      </Panel>
      <PaperScoreboard mode={m} />
      <MakerRounds mode={m} />
      <ArbPairs mode={m} />
      <StrikesPanel mode={m} />
      <WeatherPanel />
      <Panel>
        <PanelHeader icon={<ListOrdered />} title="Order log" />
        {!orders.data?.length ? <Empty>No orders yet.</Empty> : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[820px] text-[12.5px]">
              <thead><tr className="border-b border-line text-left font-num text-[10.5px] tracking-wider text-tx-low uppercase">
                {["Time", "Asset", "Purpose", "Side", "Type", "Limit", "Requested", "Filled", "Fees", "Status"].map((h) => <th key={h} className="px-3 py-2">{h}</th>)}
              </tr></thead>
              <tbody>{orders.data.map((o) => (
                <tr key={o.id} className="border-b border-line/60 align-top">
                  <td className="num px-3 py-2 whitespace-nowrap text-tx-dim">{o.ts ? new Date(o.ts).toLocaleTimeString() : ""}</td>
                  <td className="px-3 py-2 font-num font-bold text-tx-hi">{o.asset.toUpperCase()}</td>
                  <td className="px-3 py-2">{o.purpose}</td><td className="px-3 py-2">{o.side}</td><td className="px-3 py-2">{o.type}</td>
                  <td className="num px-3 py-2">{fmtCents(o.limit)}</td>
                  <td className="num px-3 py-2">{o.side === "BUY" ? fmtUsd(o.requested) : `${o.requested.toFixed(2)} sh`}</td>
                  <td className="num px-3 py-2">{o.filled_shares ? `${o.filled_shares.toFixed(2)} sh · ${fmtUsd(o.filled_cost)}` : "—"}</td>
                  <td className="num px-3 py-2 text-tx-low">{fmtUsd(o.fees)}</td>
                  <td className="px-3 py-2"><span className={cn("rounded-full border px-2 py-0.5 font-num text-[10px] font-bold uppercase",
                    o.status === "filled" ? "border-up/50 text-up" : o.status === "rejected" ? "border-down/50 text-down" : "border-line-hi text-tx-low")}>{o.status}</span>
                    {o.error && <div className="mt-1 max-w-56 text-[11px] text-tx-low">{o.error}</div>}</td>
                </tr>))}</tbody>
            </table>
          </div>
        )}
      </Panel>
    </div>
  );
}
