import { useQuery } from "@tanstack/react-query";
import { motion } from "framer-motion";
import { Activity, BookOpen, Bitcoin, Briefcase, Layers, LayoutGrid, LineChart } from "lucide-react";
import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { useNow } from "@/components/layout/Shell";
import { Badge, Empty, Panel, PanelHeader, Stat } from "@/components/ui/panel";
import { useOrderbook, type Book } from "@/hooks/useOrderbook";
import { usePortfolioStream } from "@/hooks/usePortfolioStream";
import { ASSETS, ASSET_META, cn, fmtCents, fmtPct, fmtPrice, fmtUsd, mmss, tone, windowStart, type Asset } from "@/lib/utils";
import { CandleChart } from "@/markets/CandleChart";
import { Sparkline } from "@/markets/Sparkline";
import { Api } from "@/services/api";
import { useMarket, windowDelta } from "@/stores/market";
import { btcRegime } from "@/strategies/consensus";

function Watchlist({ selected, onSelect }: { selected: Asset; onSelect: (a: Asset) => void }) {
  const spot = useMarket((s) => s.spot);
  const live = useMarket((s) => s.status === "live");
  return (
    <Panel>
      <PanelHeader icon={<Layers />} title="Watchlist" right={<Badge className={live ? "border-up/50 text-up" : "border-down/50 text-down"}>{live ? "6 live" : "offline"}</Badge>} />
      <div className="space-y-2 p-3">
        {ASSETS.map((a) => {
          const s = spot[a], m = ASSET_META[a];
          return (
            <button key={a} onClick={() => onSelect(a)} aria-pressed={selected === a}
              className={cn("flex w-full items-center gap-3 rounded-2xl border px-3 py-2.5 text-left transition-colors",
                selected === a ? "border-amber-hi/50 bg-amber/10" : "border-line bg-surf-1/50 hover:border-line-hi")}>
              <span className="w-5 text-center text-lg" style={{ color: m.color }}>{m.glyph}</span>
              <span className="min-w-0 flex-1">
                <span className="block font-num text-[13px] font-bold text-tx-hi">{m.sym}</span>
                <span className="block truncate text-[10.5px] tracking-wider text-tx-low uppercase">{m.name}</span>
              </span>
              <Sparkline values={s.candles.slice(-60).map((c) => c.close)} color={m.color} w={52} />
              <span className="w-20 text-right">
                <span className="num block text-[13px] font-semibold text-tx-hi">${fmtPrice(s.price)}</span>
                <span className={cn("num block text-[11px]", tone(s.change24h))}>{fmtPct(s.change24h)}</span>
              </span>
            </button>
          );
        })}
      </div>
    </Panel>
  );
}

function BtcRegime() {
  const btc = useMarket((s) => s.spot.btc);
  const r = btcRegime(btc.change1h);
  const fill = btc.change1h == null ? 0 : Math.min(100, Math.max(0, 50 + (btc.change1h / 4) * 100));
  return (
    <Panel>
      <PanelHeader icon={<Bitcoin />} title="BTC regime (lead)" />
      <dl className="space-y-2.5 px-4 py-4 text-[12px]">
        <div className="flex justify-between"><dt className="tracking-wider text-tx-low uppercase">BTC/USD</dt><dd className="num font-semibold text-tx-hi">${fmtPrice(btc.price)}</dd></div>
        <div className="flex justify-between"><dt className="tracking-wider text-tx-low uppercase">1h change</dt><dd className={cn("num font-semibold", tone(btc.change1h))}>{fmtPct(btc.change1h)}</dd></div>
        <div className="flex justify-between"><dt className="tracking-wider text-tx-low uppercase">Filter state</dt>
          <dd className={cn("font-num font-bold", r.state === "NORMAL" ? "text-amber-hi" : r.state === "UNKNOWN" ? "text-tx-dim" : "text-down")}>{r.state}</dd></div>
        <div className="relative mt-1 h-1.5 rounded-full bg-surf-3">
          <div className="absolute inset-y-0 left-0 rounded-full bg-gradient-to-r from-azure via-amber to-down" style={{ width: `${fill}%` }} />
        </div>
        <p className="pt-1 text-[11.5px] leading-relaxed text-tx-low">
          Reversion filter: a BTC 1h change of +2% or more pulls the DOWN threshold to 0.06%; −2% or less pulls the UP threshold to 0.03%.
        </p>
      </dl>
    </Panel>
  );
}

function YourNumbers() {
  const pf = useQuery({ queryKey: ["portfolio"], queryFn: () => Api.portfolio(), refetchInterval: 10_000 });
  const p = pf.data;
  const cell = (l: string, v: string, t?: string) => (
    <div><div className="panel-title !text-[10px]">{l}</div><div className={cn("num mt-1 text-xl font-semibold text-tx-hi", t)}>{v}</div></div>
  );
  return (
    <Panel>
      <PanelHeader icon={<LineChart />} title="Your numbers" right={<Badge className={p?.mode === "live" ? "border-up/50 text-up" : "border-azure/40 text-azure"}>{p?.mode ?? "paper"}</Badge>} />
      <div className="grid grid-cols-2 gap-4 px-4 py-4">
        {cell("Equity", fmtUsd(p?.equity))}
        {cell("Invested now", fmtUsd(p?.invested))}
        {cell("Today", fmtUsd(p?.today_realized, true), tone(p?.today_realized))}
        {cell("Peak equity", fmtUsd(p?.peak_equity))}
      </div>
    </Panel>
  );
}

function Kpis() {
  const pf = useQuery({ queryKey: ["portfolio"], queryFn: () => Api.portfolio(), refetchInterval: 10_000 });
  const btc = useMarket((s) => s.spot.btc);
  const regime = btcRegime(btc.change1h);
  const now = useNow();
  const left = windowStart() + 300 - now / 1000;
  const p = pf.data;
  const total = p ? p.realized_pnl + p.unrealized_pnl : null;
  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 min-[1700px]:grid-cols-6">
      <Stat label="Bankroll" value={fmtUsd(p?.cash)} sub={p ? `${p.mode} · + positions ${fmtUsd(p.positions_value)}` : ""} accent="#5cc8ff" />
      <Stat label="Total P&L" value={fmtUsd(total, true)} tone={tone(total)} sub={p ? `realized ${fmtUsd(p.realized_pnl, true)}` : ""} accent="#35d49a" />
      <Stat label={`Trades (${p?.trades ?? 0})`} value={p?.trades ? `${p.wins}W / ${p.trades - p.wins}L` : "—"} sub={p ? `${p.open_positions} open` : ""} accent="#c6b28c" />
      <Stat label="Win rate" value={p?.win_rate != null ? `${(p.win_rate * 100).toFixed(1)}%` : "—"} sub={p ? `drawdown ${p.drawdown_pct.toFixed(1)}%` : ""} accent="#f79240" />
      <Stat label="BTC filter" value={regime.state === "UNKNOWN" ? "—" : regime.state} tone={regime.state === "NORMAL" ? "text-up" : "text-down"} sub={fmtPct(btc.change1h)} accent="#ffd36e" />
      <Stat label="Round left" value={mmss(left)} sub="Chainlink settles" accent="#ff5a76" />
    </div>
  );
}

function BookSide({ label, book, color }: { label: string; book: Book | undefined; color: string }) {
  const asks = (book?.asks ?? []).slice(0, 5).reverse();
  const bids = (book?.bids ?? []).slice(0, 5);
  const max = Math.max(1, ...asks.map((l) => l.size), ...bids.map((l) => l.size));
  const row = (l: { price: number; size: number }, side: "a" | "b") => (
    <div key={side + l.price} className="relative flex justify-between px-3 py-[3px] text-[11.5px]">
      <span className={cn("absolute inset-y-0 right-0", side === "a" ? "bg-down/12" : "bg-up/12")} style={{ width: `${(l.size / max) * 100}%` }} />
      <span className={cn("num relative", side === "a" ? "text-down" : "text-up")}>{fmtCents(l.price)}</span>
      <span className="num relative text-tx">{l.size.toFixed(0)}</span>
    </div>
  );
  return (
    <div className="min-w-0 flex-1">
      <div className="flex items-baseline justify-between px-3 pb-1"><span className="font-num text-[11px] font-bold" style={{ color }}>{label}</span>
        <span className="num text-[10.5px] text-tx-low">spread {book?.spread != null ? fmtCents(book.spread) : "—"}</span></div>
      {asks.map((l) => row(l, "a"))}
      <div className="my-1 border-t border-dashed border-line-mid" />
      {bids.map((l) => row(l, "b"))}
    </div>
  );
}

function OrderBook({ asset }: { asset: Asset }) {
  const m = useMarket((s) => s.spot[asset].market);
  const books = useOrderbook(m?.id);
  return (
    <Panel>
      <PanelHeader icon={<BookOpen />} title="Order book" right={<span className="font-num text-xs font-bold text-tx-mid">{ASSET_META[asset].sym}</span>} />
      {!m ? <Empty>{useMarket.getState().status === "live" ? "No active 5-minute market found for this round yet." : "Waiting for the market feed…"}</Empty> : (
        <div className="py-3">
          <div className="flex gap-2"><BookSide label="UP" book={books.data?.up} color="#35d49a" />
            <BookSide label="DOWN" book={books.data?.down} color="#ff5a76" /></div>
          <a href={`https://polymarket.com/event/${m.slug}`} target="_blank" rel="noreferrer" className="mt-2 block truncate px-3 text-[10.5px] text-tx-dim hover:text-tx">{m.question} ↗</a>
        </div>
      )}
    </Panel>
  );
}

function OpenPositions() {
  const pf = useQuery({ queryKey: ["portfolio"], queryFn: () => Api.portfolio(), refetchInterval: 10_000 });
  const mode = pf.data?.mode ?? "paper";
  const q = useQuery({ queryKey: ["positions", mode], queryFn: () => Api.positions(mode), refetchInterval: 3000 });
  return (
    <Panel>
      <PanelHeader icon={<Briefcase />} title="Positions open" right={<Link to="/history" className="font-num text-[11px] font-semibold tracking-wider text-tx-mid uppercase hover:text-amber">History ›</Link>} />
      {!q.data?.length ? <Empty>Nothing open right now.</Empty> : (
        <div className="divide-y divide-line">
          {q.data.map((p) => (
            <div key={p.id} className="px-4 py-2.5 text-[12px]">
              <div className="flex items-center justify-between">
                <span className="font-num font-bold text-tx-hi">{p.asset.toUpperCase()} <span className={p.side === "UP" ? "text-up" : "text-down"}>{p.side}</span>
                  <span className="ml-2 font-sans text-[11px] font-normal text-tx-low">{p.strategy}{p.dca_steps > 1 ? ` · DCA ×${p.dca_steps - 1}` : ""}</span></span>
                <span className={cn("num font-semibold", tone(p.unrealized_pnl))}>{p.status === "awaiting_resolution" ? "settling…" : fmtUsd(p.unrealized_pnl, true)}</span>
              </div>
              <div className="num mt-0.5 flex justify-between text-tx-low">
                <span>{p.shares.toFixed(2)} sh @ {fmtCents(p.avg_price)} · cost {fmtUsd(p.cost_basis)}</span><span>bid {fmtCents(p.bid)}</span>
              </div>
            </div>
          ))}
        </div>
      )}
    </Panel>
  );
}

function ActivityLog() {
  const logs = useQuery({ queryKey: ["logs", "all"], queryFn: () => Api.logs("all"), refetchInterval: 5000 });
  return (
    <Panel className="flex h-[560px] flex-col">
      <PanelHeader icon={<Activity />} title="Activity log" right={<Link to="/logs" className="font-num text-[11px] font-semibold tracking-wider text-tx-mid uppercase hover:text-amber">Expand ›</Link>} />
      <div className="min-h-0 flex-1 overflow-y-auto">
        {logs.data?.length ? logs.data.slice(0, 40).map((l) => (
          <div key={l.id} className="border-b border-dashed border-line px-4 py-1.5 text-[12px]">
            <span className="num mr-2 text-tx-dim">{new Date(l.ts).toLocaleTimeString()}</span>
            <span className={cn(l.level === "error" ? "text-down" : l.level === "warn" ? "text-warn" : "text-tx")}>{l.message}</span>
          </div>
        )) : <Empty>No engine activity yet. Enable a strategy in Settings and press Start bot.</Empty>}
      </div>
    </Panel>
  );
}

export default function Terminal() {
  usePortfolioStream();
  const [asset, setAsset] = useState<Asset>("btc");
  const s = useMarket((st) => st.spot[asset]);
  const delta = windowDelta(s);
  const candles = useMemo(() => s.candles, [s.candles]);
  return (
    <div className="grid gap-4 xl:grid-cols-[300px_minmax(0,1fr)_330px]">
      <div className="space-y-4"><Watchlist selected={asset} onSelect={setAsset} /><BtcRegime /><YourNumbers /></div>
      <div className="flex min-w-0 flex-col gap-4">
        <Panel className="p-3"><Kpis /></Panel>
        <Panel className="relative flex h-[620px] flex-col p-4">
          <div className="flex items-start justify-between gap-3">
            <motion.div key={asset} initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }} className="glass px-5 py-3.5">
              <div className="font-num text-lg font-bold text-tx-hi">{ASSET_META[asset].sym}/USD</div>
              <div className={cn("num text-3xl font-semibold", tone(delta))}>${fmtPrice(s.price)}</div>
              <div className="mt-1 font-num text-[10.5px] tracking-[0.18em] text-tx-low uppercase">5 minute · Polymarket · round {fmtPct(delta, 3)}</div>
            </motion.div>
            <div className="flex flex-wrap justify-end gap-1.5" role="group" aria-label="chart asset">
              <LayoutGrid className="mt-2 mr-1 h-4 w-4 text-azure" />
              {ASSETS.map((a) => (
                <button key={a} onClick={() => setAsset(a)} aria-pressed={asset === a}
                  className={cn("rounded-full border px-2.5 py-1 font-num text-[11px] font-bold", asset === a ? "border-amber/60 bg-amber/15 text-amber" : "border-line-mid text-tx-low hover:text-tx")}>
                  {ASSET_META[a].sym}
                </button>
              ))}
            </div>
          </div>
          <div className="mt-3 min-h-[340px] flex-1">
            {candles.length ? <CandleChart key={asset} candles={candles} windowOpen={s.windowOpen} /> :
              <div className="flex h-full items-center justify-center font-num text-xs tracking-[0.2em] text-tx-low uppercase">Loading {ASSET_META[asset].sym} candles…</div>}
          </div>
        </Panel>
      </div>
      <div className="flex flex-col gap-4">
        <OrderBook asset={asset} />
        <OpenPositions />
        <ActivityLog />
      </div>
    </div>
  );
}
