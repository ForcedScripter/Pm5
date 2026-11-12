import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AreaSeries, ColorType, createChart, type UTCTimestamp } from "lightweight-charts";
import { AlertTriangle, Database, FlaskConical, Play, Trash2 } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Chips, NumberField, Segmented } from "@/components/ui/form";
import { Badge, Empty, Panel, PanelHeader } from "@/components/ui/panel";
import { cn, fmtCents, fmtUsd, tone } from "@/lib/utils";
import { Api } from "@/services/api";
import type { BacktestResults, Metrics } from "@/types/api";

const STRATS = [
  { value: "binance_lead", label: "Binance Lead" },
  { value: "twap_lock", label: "TWAP Lock" },
  { value: "constellation", label: "Constellation" },
  { value: "odds_scalper", label: "Odds Scalper" },
  { value: "pocket_aces", label: "Pocket Aces" },
  { value: "custom_btc", label: "Custom BTC" },
];
const pct = (v?: number | null, d = 1) => (v == null ? "—" : `${(v * 100).toFixed(d)}%`);
const num = (v?: number | null, d = 2) => (v == null ? "—" : v.toFixed(d));

function DataPanel() {
  const qc = useQueryClient();
  const cov = useQuery({ queryKey: ["coverage"], queryFn: Api.coverage, refetchInterval: 15_000 });
  const bf = useQuery({ queryKey: ["backfill"], queryFn: Api.backfill, refetchInterval: (q) => (q.state.data?.running ? 2000 : 15_000) });
  const [days, setDays] = useState<"1" | "3" | "7" | "14">("3");
  const start = useMutation({ mutationFn: () => Api.startBackfill(Number(days)), onSuccess: () => qc.invalidateQueries({ queryKey: ["backfill"] }) });
  const c = cov.data, b = bf.data;
  return (
    <Panel>
      <PanelHeader icon={<Database />} title="History available" />
      <div className="space-y-3 p-4 text-[12.5px]">
        <div className="grid grid-cols-3 gap-2 text-center">
          {[["Settled rounds", c?.settled_rounds], ["Recorded", c?.recorded_rounds], ["Backfilled", c?.backfilled_rounds]].map(([l, v]) => (
            <div key={l as string} className="rounded-xl border border-line bg-bg-0/40 py-2"><div className="panel-title !text-[9.5px]">{l}</div>
              <div className="num text-lg font-semibold text-tx-hi">{v ?? "—"}</div></div>
          ))}
        </div>
        <p className="text-tx-low">{c?.first_round ? `${new Date(c.first_round).toLocaleString()} → ${new Date(c.last_round!).toLocaleString()}` : ""}
          {" "}· <b className="text-tx">Recorded</b> rounds have real 5-second books. <b className="text-tx">Backfilled</b> ones have one public price per minute that
          lags the real market, so they overstate momentum strategies: use them to explore, not to judge an edge.</p>
        <div className="flex flex-wrap items-center gap-2">
          <div className="w-56"><Segmented value={days} onChange={setDays} options={[{ value: "1", label: "1d" }, { value: "3", label: "3d" }, { value: "7", label: "7d" }, { value: "14", label: "14d" }]} /></div>
          <Button variant="amber" size="sm" loading={start.isPending} disabled={b?.running} onClick={() => start.mutate()}>Backfill history</Button>
        </div>
        {b?.running && (
          <div><div className="mb-1 flex justify-between text-[11px] text-tx-low"><span>{b.stage === "bars" ? "downloading 1s spot bars…" : `rounds ${b.done}/${b.total}`}</span>
            <span>{b.total ? pct((b.done ?? 0) / b.total, 0) : ""}</span></div>
            <div className="h-1.5 rounded-full bg-surf-3"><div className="h-full rounded-full bg-amber" style={{ width: `${b.total ? ((b.done ?? 0) / b.total) * 100 : 3}%` }} /></div></div>
        )}
        {!b?.running && b?.stage === "done" && <p className="text-[11px] text-up">Last backfill: {Object.entries(b.counts ?? {}).map(([k, v]) => `${v} ${k}`).join(" · ")} in {b.seconds}s</p>}
        {(start.error || b?.error) && <p className="text-[11px] text-down">{start.error?.message ?? b?.error}</p>}
      </div>
    </Panel>
  );
}

function RunForm({ onStarted }: { onStarted: (id: string) => void }) {
  const qc = useQueryClient();
  const [strategies, setStrategies] = useState<string[]>(["constellation", "custom_btc", "pocket_aces"]);
  const [days, setDays] = useState<number | null>(3);
  const [fidelity, setFidelity] = useState<"auto" | "recorded" | "backfill">("recorded");
  const [bankroll, setBankroll] = useState<number | null>(200);
  const [latency, setLatency] = useState<number | null>(2);
  const [slip, setSlip] = useState<number | null>(0);
  const [riskMode, setRiskMode] = useState<"mine" | "edge">("mine");
  // "edge": lift only the PORTFOLIO caps so each strategy is judged on its own signals;
  // per-trade sizing, book gates, fees and latency rules still apply
  const EDGE_RISK = { max_daily_exposure_usd: 1e9, max_daily_loss_usd: 1e9, max_open_positions: 50, max_drawdown_pct: 100, loss_cooldown_s: 0 };
  const run = useMutation({
    mutationFn: () => Api.runBacktest({ strategies, days, fidelity, bankroll, latency_s: latency, extra_slippage: slip,
      risk: riskMode === "edge" ? EDGE_RISK : {}, name: undefined }),
    onSuccess: (r) => { qc.invalidateQueries({ queryKey: ["backtests"] }); onStarted(r.id); },
  });
  return (
    <Panel>
      <PanelHeader icon={<FlaskConical />} title="New backtest" />
      <div className="space-y-3.5 p-4">
        <Chips values={strategies} options={STRATS} onToggle={(v) => setStrategies((s) => (s.includes(v) ? s.filter((x) => x !== v) : [...s, v]))} />
        <p className="text-[11px] text-tx-dim">Selected strategies share one bankroll and your risk limits, using their saved settings.</p>
        <div className="space-y-2">
          <NumberField label="Period (days back)" step={0.5} min={0.25} max={60} value={days} onChange={setDays} />
          <NumberField label="Bankroll" suffix="$" step={10} value={bankroll} onChange={setBankroll} />
          <NumberField label="Latency" hint="A decision at t executes at the first book at or after t + latency (never at t)." suffix="s" step={1} value={latency} onChange={setLatency} />
          <NumberField label="Extra slippage" hint="Added to every fill price on top of the book's ask/bid." step={0.005} value={slip} onChange={setSlip} />
        </div>
        <Segmented value={riskMode} onChange={setRiskMode} options={[{ value: "mine", label: "My risk limits", sub: "what the bot would do" }, { value: "edge", label: "Strategy edge", sub: "no portfolio caps" }]} />
        <Segmented value={fidelity} onChange={setFidelity} options={[{ value: "recorded", label: "Recorded", sub: "real books · trustworthy" }, { value: "auto", label: "Mixed", sub: "adds backfill · biased" }, { value: "backfill", label: "Backfill", sub: "biased · exploratory" }]} />
        {run.error && <p className="text-sm text-down" role="alert">{run.error.message}</p>}
        <Button variant="gold" className="w-full" loading={run.isPending} disabled={!strategies.length} onClick={() => run.mutate()}><Play className="h-4 w-4" />Run backtest</Button>
      </div>
    </Panel>
  );
}

function Equity({ curve, bankroll }: { curve: { t: number; equity: number }[]; bankroll: number }) {
  const el = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!el.current || !curve.length) return;
    const c = createChart(el.current, { autoSize: true, layout: { background: { type: ColorType.Solid, color: "transparent" }, textColor: "#a8987a", fontFamily: "Space Grotesk" },
      grid: { vertLines: { color: "rgba(198,178,140,0.06)" }, horzLines: { color: "rgba(198,178,140,0.06)" } }, timeScale: { timeVisible: true } });
    const up = curve[curve.length - 1].equity >= bankroll;
    const s = c.addSeries(AreaSeries, { lineColor: up ? "#35d49a" : "#ff5a76", topColor: up ? "rgba(53,212,154,0.25)" : "rgba(255,90,118,0.25)", bottomColor: "rgba(0,0,0,0)", lineWidth: 2 });
    const seen = new Set<number>();
    s.setData(curve.filter((p) => (seen.has(Math.floor(p.t)) ? false : (seen.add(Math.floor(p.t)), true))).map((p) => ({ time: Math.floor(p.t) as UTCTimestamp, value: p.equity })));
    s.createPriceLine({ price: bankroll, color: "#f2c45a", lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: "start" });
    c.timeScale().fitContent();
    return () => c.remove();
  }, [curve, bankroll]);
  return <div ref={el} className="h-64 w-full" />;
}

function MetricTable({ rows }: { rows: [string, Metrics][] }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[760px] text-[12.5px]">
        <thead><tr className="border-b border-line text-left font-num text-[10.5px] tracking-wider text-tx-low uppercase">
          {["", "Trades", "Win rate (95% CI)", "Break-even", "Net P&L", "Profit factor", "EV/trade", "ROI on $ staked", "Max DD"].map((h) => <th key={h} className="px-3 py-2">{h}</th>)}
        </tr></thead>
        <tbody>{rows.map(([k, m]) => (
          <tr key={k} className="border-b border-line/60">
            <td className="px-3 py-2 font-semibold text-tx-hi">{k}</td><td className="num px-3 py-2">{m.trades}</td>
            <td className={cn("num px-3 py-2", m.win_rate != null && m.breakeven_win_rate != null && (m.win_rate >= m.breakeven_win_rate ? "text-up" : "text-down"))}>
              {pct(m.win_rate)} {m.win_rate_ci95 ? <span className="text-tx-dim">({pct(m.win_rate_ci95[0], 0)}–{pct(m.win_rate_ci95[1], 0)})</span> : null}</td>
            <td className="num px-3 py-2 text-tx-low">{pct(m.breakeven_win_rate)}</td>
            <td className={cn("num px-3 py-2 font-semibold", tone(m.net_pnl))}>{fmtUsd(m.net_pnl, true)}</td>
            <td className="num px-3 py-2">{num(m.profit_factor)}</td><td className={cn("num px-3 py-2", tone(m.expectancy))}>{fmtUsd(m.expectancy, true)}</td>
            <td className={cn("num px-3 py-2", tone(m.roi_on_deployed))}>{pct(m.roi_on_deployed)}</td><td className="num px-3 py-2">{fmtUsd(m.max_drawdown)} ({num(m.max_drawdown_pct, 1)}%)</td>
          </tr>))}</tbody>
      </table>
    </div>
  );
}

function Results({ r, bankroll }: { r: BacktestResults; bankroll: number }) {
  const o = r.overall;
  const tile = (l: string, v: string, sub?: string, t?: string) => (
    <div className="rounded-2xl border border-line bg-bg-0/40 px-4 py-3"><div className="panel-title !text-[10px]">{l}</div>
      <div className={cn("num mt-1 text-lg font-semibold text-tx-hi", t)}>{v}</div>{sub && <div className="text-[10.5px] text-tx-low">{sub}</div>}</div>
  );
  return (
    <div className="space-y-4">
      {r.warnings.length > 0 && (
        <div className="space-y-1.5 rounded-2xl border border-warn/40 bg-warn/10 px-4 py-3 text-[12.5px] text-warn">
          {r.warnings.map((w) => <p key={w} className="flex gap-2"><AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />{w}</p>)}
        </div>
      )}
      {!o.trades ? <Empty>No trades in this period with these settings.</Empty> : (<>
        <div className="grid grid-cols-2 gap-2.5 md:grid-cols-5">
          {tile("Net P&L", fmtUsd(o.net_pnl, true), `${pct(o.total_return)} of bankroll · after fees`, tone(o.net_pnl))}
          {tile("Trades", String(o.trades), `${num(o.trades_per_day, 1)}/day over ${num(o.span_days, 1)} days`)}
          {tile("Win rate", pct(o.win_rate), `break-even ${pct(o.breakeven_win_rate)}`, o.win_rate! >= (o.breakeven_win_rate ?? 0) ? "text-up" : "text-down")}
          {tile("Profit factor", num(o.profit_factor), "gross win ÷ gross loss")}
          {tile("Max drawdown", fmtUsd(o.max_drawdown), `${num(o.max_drawdown_pct, 1)}% of peak`)}
          {tile("EV per trade", fmtUsd(o.expectancy, true), `avg win ${fmtUsd(o.avg_win)} · avg loss ${fmtUsd(o.avg_loss)}`, tone(o.expectancy))}
          {tile("ROI on $ staked", pct(o.roi_on_deployed), `${fmtUsd(o.deployed)} staked`)}
          {tile("Sharpe", num(o.sharpe_daily_annualized ?? o.sharpe_per_trade), o.sharpe_daily_annualized != null ? "daily, annualized" : "per trade (< 5 days)")}
          {tile("Fees paid", fmtUsd(o.fees), "Polymarket taker fees")}
          {tile("Rounds", String(r.rounds.total), `${r.rounds.recorded ?? 0} recorded · ${r.rounds.backfill ?? 0} backfilled`)}
        </div>
        <Panel className="p-3"><Equity curve={o.equity_curve ?? []} bankroll={bankroll} /></Panel>
        {r.split && <div><div className="panel-title mb-2">In-sample vs out-of-sample <span className="normal-case tracking-normal text-tx-dim">(split at {new Date(r.split.split_at * 1000).toLocaleString()}: the last 30% of the period)</span></div>
          <Panel><MetricTable rows={[["In-sample", r.split.in_sample], ["Out-of-sample", r.split.out_of_sample]]} /></Panel></div>}
        <div><div className="panel-title mb-2">By strategy</div><Panel><MetricTable rows={Object.entries(r.by_strategy)} /></Panel></div>
        <div><div className="panel-title mb-2">By asset</div><Panel><MetricTable rows={Object.entries(r.by_asset)} /></Panel></div>
      </>)}
      <div className="grid gap-4 md:grid-cols-3 text-[12px]">
        {[["Pipeline", r.counts], ["Risk blocks", r.risk_blocks], ["Top skip reasons", r.skips]].map(([title, obj]) => (
          <Panel key={title as string}><PanelHeader title={title as string} />
            <ul className="space-y-1 p-4">{Object.keys(obj as object).length ? Object.entries(obj as Record<string, number>).slice(0, 12).map(([k, v]) => <li key={k} className="flex justify-between gap-3"><span className="truncate text-tx">{k}</span><span className="num text-tx-hi">{v}</span></li>) : <li className="text-tx-dim">none</li>}</ul>
          </Panel>))}
      </div>
      {r.trades.length > 0 && (
        <Panel><PanelHeader title={`Trades (${r.trades.length > 200 ? "last 200 of " : ""}${r.trades.length})`} />
          <div className="max-h-[420px] overflow-auto">
            <table className="w-full min-w-[760px] text-[12px]"><thead className="sticky top-0 bg-surf-2"><tr className="text-left font-num text-[10.5px] tracking-wider text-tx-low uppercase">
              {["Round", "Strategy", "Asset", "Side", "Entry", "Cost", "Fees", "P&L", "Result", "Data"].map((h) => <th key={h} className="px-3 py-2">{h}</th>)}</tr></thead>
              <tbody>{r.trades.slice(-200).reverse().map((t, i) => (
                <tr key={i} className="border-b border-line/60"><td className="num px-3 py-1.5 text-tx-dim">{new Date(t.opened_at * 1000).toLocaleString()}</td>
                  <td className="px-3 py-1.5">{t.strategy}</td><td className="px-3 py-1.5 font-num font-bold">{t.asset.toUpperCase()}</td>
                  <td className={cn("px-3 py-1.5 font-num font-bold", t.side === "UP" ? "text-up" : "text-down")}>{t.side}</td>
                  <td className="num px-3 py-1.5">{fmtCents(t.entry_price)}{t.dca_stages > 1 ? ` +DCA×${t.dca_stages - 1}` : ""}</td>
                  <td className="num px-3 py-1.5">{fmtUsd(t.cost)}</td><td className="num px-3 py-1.5 text-tx-low">{fmtUsd(t.fees)}</td>
                  <td className={cn("num px-3 py-1.5 font-semibold", tone(t.pnl))}>{fmtUsd(t.pnl, true)}</td><td className="px-3 py-1.5">{t.result}</td>
                  <td className="px-3 py-1.5 text-tx-low">{t.fidelity}</td></tr>))}</tbody></table>
          </div></Panel>
      )}
    </div>
  );
}

export default function Backtest() {
  const qc = useQueryClient();
  const list = useQuery({ queryKey: ["backtests"], queryFn: Api.backtests, refetchInterval: (q) => (q.state.data?.some((b) => b.status === "running" || b.status === "queued") ? 1500 : 20_000) });
  const [sel, setSel] = useState<string | null>(null);
  const id = sel ?? list.data?.[0]?.id ?? null;
  const detail = useQuery({ queryKey: ["backtest", id], queryFn: () => Api.backtest(id!), enabled: !!id,
    refetchInterval: (q) => (q.state.data && (q.state.data.status === "running" || q.state.data.status === "queued") ? 1500 : false) });
  const del = useMutation({ mutationFn: (x: string) => Api.deleteBacktest(x), onSuccess: () => { setSel(null); qc.invalidateQueries({ queryKey: ["backtests"] }); } });
  const d = detail.data;
  return (
    <div className="grid grid-cols-[minmax(0,1fr)] gap-5 xl:grid-cols-[360px_minmax(0,1fr)]">
      <div className="space-y-5">
        <DataPanel />
        <RunForm onStarted={setSel} />
        <Panel><PanelHeader title="Runs" />
          {!list.data?.length ? <Empty>No backtests yet.</Empty> : (
            <ul className="divide-y divide-line">{list.data.map((b) => (
              <li key={b.id}><button onClick={() => setSel(b.id)} className={cn("w-full px-4 py-2.5 text-left text-[12px] hover:bg-surf-2/50", b.id === id && "bg-amber/10")}>
                <div className="flex justify-between gap-2"><span className="min-w-0 truncate font-semibold text-tx-hi">{b.name.replaceAll("+", " + ")}</span>
                  <Badge className={b.status === "done" ? "border-up/40 text-up" : b.status === "failed" ? "border-down/50 text-down" : "border-amber/50 text-amber"}>{b.status === "running" ? pct(b.progress, 0) : b.status}</Badge></div>
                <div className="num mt-0.5 text-tx-low">{b.trades != null ? `${b.trades} trades · ${fmtUsd(b.net_pnl, true)} · win ${pct(b.win_rate)}` : b.error ?? ""}</div>
              </button></li>))}</ul>)}
        </Panel>
      </div>
      <div className="min-w-0">
        {!d ? <Panel><Empty>Run a backtest to see results.</Empty></Panel> : (
          <Panel className="p-5">
            <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
              <div className="min-w-0"><h2 className="font-brand text-xl break-words text-tx-hi">{d.name.replaceAll("+", " + ")}</h2>
                <p className="text-[12px] text-tx-low">{new Date(d.period[0]).toLocaleString()} → {new Date(d.period[1]).toLocaleString()} · {d.strategies.join(", ")}</p></div>
              <Button variant="ghost" size="sm" disabled={d.status === "running"} onClick={() => del.mutate(d.id)}><Trash2 className="h-3.5 w-3.5" />Delete</Button>
            </div>
            {d.status === "failed" ? <p className="text-sm text-down">{d.error}</p> :
              d.status !== "done" || !d.results ? (
                <div className="py-10"><div className="mx-auto h-2 max-w-md rounded-full bg-surf-3"><div className="h-full rounded-full bg-amber transition-all" style={{ width: `${Math.max(3, d.progress * 100)}%` }} /></div>
                  <p className="mt-3 text-center text-[12px] text-tx-low">Replaying {pct(d.progress, 0)}…</p></div>
              ) : <Results r={d.results} bankroll={Number(d.params.bankroll ?? 200)} />}
          </Panel>
        )}
      </div>
    </div>
  );
}
