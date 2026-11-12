import * as Tabs from "@radix-ui/react-tabs";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, Cog, Gauge, OctagonX, RotateCcw, Server, ShieldAlert, XCircle } from "lucide-react";
import { useEffect, useState } from "react";
import { useLocation } from "react-router-dom";
import { Button } from "@/components/ui/button";
import { NumberField } from "@/components/ui/form";
import { Badge, Empty, Panel, PanelHeader } from "@/components/ui/panel";
import { cn } from "@/lib/utils";
import { Api } from "@/services/api";
import { StrategyEditor } from "@/strategies/StrategyEditor";
import type { Acceptance, RiskSettings } from "@/types/api";
import { WalletPanel } from "@/wallet/WalletPanel";

const CHECK_LABEL: Record<string, string> = {
  server_allows_live: "Server allows live (ALLOW_LIVE_TRADING)",
  wallet_connected: "Wallet connected",
  not_emergency_stopped: "Emergency stop cleared",
  paper_days: "Paper trading duration",
  paper_trades: "Paper trade count",
  paper_win_rate: "Paper win rate",
  paper_profit_factor: "Paper profit factor",
};

function LiveTrading() {
  const qc = useQueryClient();
  const s = useQuery({ queryKey: ["settings"], queryFn: Api.settings });
  const r = useQuery({ queryKey: ["readiness"], queryFn: Api.readiness, refetchInterval: 15_000 });
  const inval = () => { qc.invalidateQueries({ queryKey: ["settings"] }); qc.invalidateQueries({ queryKey: ["readiness"] }); };
  const enable = useMutation({ mutationFn: Api.liveEnable, onSuccess: inval });
  const disable = useMutation({ mutationFn: Api.liveDisable, onSuccess: inval });
  const clear = useMutation({ mutationFn: Api.emergencyClear, onSuccess: inval });
  const live = s.data?.live_trading_enabled;
  const p = r.data?.paper, c = r.data?.criteria;
  const detail: Record<string, string> = p && c ? {
    paper_days: `${p.days.toFixed(1)} / ${c.min_paper_days} days`, paper_trades: `${p.trades} / ${c.min_paper_trades}`,
    paper_win_rate: `${(p.win_rate * 100).toFixed(1)}% / ${(c.min_win_rate * 100).toFixed(0)}%`,
    paper_profit_factor: `${p.profit_factor >= 999 ? "∞" : p.profit_factor.toFixed(2)} / ${c.min_profit_factor}`,
  } : {};
  return (
    <Panel>
      <PanelHeader icon={<ShieldAlert />} title="Live trading" right={<Badge className={live ? "border-up/60 text-up" : "border-amber/50 text-amber"}>{live ? "LIVE" : "PAPER"}</Badge>} />
      <div className="space-y-4 p-5">
        {s.data?.emergency_stopped && (
          <div className="flex items-center justify-between gap-3 rounded-2xl border border-down/50 bg-down/10 px-4 py-3">
            <span className="flex items-center gap-2 text-sm text-down"><OctagonX className="h-4 w-4" />Emergency stop is active</span>
            <Button size="sm" variant="outline" loading={clear.isPending} onClick={() => clear.mutate()}>Clear</Button>
          </div>
        )}
        <ul className="space-y-1.5">
          {Object.entries(r.data?.checks ?? {}).map(([k, ok]) => (
            <li key={k} className="flex items-center justify-between gap-3 text-[13px]">
              <span className="flex items-center gap-2">{ok ? <CheckCircle2 className="h-4 w-4 text-up" /> : <XCircle className="h-4 w-4 text-down" />}{CHECK_LABEL[k] ?? k}</span>
              {detail[k] && <span className="num text-xs text-tx-low">{detail[k]}</span>}
            </li>
          ))}
        </ul>
        {(enable.error || disable.error) && <p className="text-sm text-down" role="alert">{(enable.error ?? disable.error)!.message}</p>}
        {live ? <Button variant="danger" className="w-full" loading={disable.isPending} onClick={() => disable.mutate()}>Switch back to paper</Button>
          : <Button variant="gold" className="w-full" disabled={!r.data?.ready} loading={enable.isPending}
              onClick={() => { if (confirm("Enable LIVE trading? Real orders will be placed with your wallet.")) enable.mutate(); }}>Enable live trading</Button>}
        <p className="text-[11.5px] leading-relaxed text-tx-low">Live needs all three locks: the server env flag, a connected wallet, and paper results that meet your acceptance criteria below.</p>
      </div>
    </Panel>
  );
}

const RISK_FIELDS: { k: keyof RiskSettings; label: string; step: number; suffix?: string; hint?: string }[] = [
  { k: "max_trade_usd", label: "Max per trade", step: 0.5, suffix: "$" },
  { k: "max_position_usd", label: "Max per position", step: 0.5, suffix: "$" },
  { k: "max_daily_loss_usd", label: "Max daily loss", step: 5, suffix: "$", hint: "Bot halts for the UTC day once realised losses reach this." },
  { k: "max_daily_exposure_usd", label: "Max daily exposure", step: 5, suffix: "$" },
  { k: "max_open_positions", label: "Max open positions", step: 1 },
  { k: "max_trades_per_market", label: "Max trades / market", step: 1 },
  { k: "max_drawdown_pct", label: "Max drawdown", step: 1, suffix: "%" },
  { k: "min_liquidity_usd", label: "Min liquidity", step: 5, suffix: "$" },
  { k: "max_spread", label: "Max spread", step: 0.005 },
  { k: "max_slippage", label: "Max slippage", step: 0.005 },
  { k: "min_seconds_before_resolution", label: "Min time left", step: 5, suffix: "s" },
  { k: "loss_cooldown_s", label: "Cooldown after losses", step: 30, suffix: "s" },
  { k: "consecutive_losses_for_cooldown", label: "Losses before cooldown", step: 1 },
];
const ACC_FIELDS: { k: keyof Acceptance; label: string; step: number }[] = [
  { k: "min_paper_days", label: "Min paper days", step: 1 },
  { k: "min_paper_trades", label: "Min paper trades", step: 10 },
  { k: "min_win_rate", label: "Min win rate (0–1)", step: 0.01 },
  { k: "min_profit_factor", label: "Min profit factor", step: 0.05 },
];

function RiskPanel() {
  const qc = useQueryClient();
  const s = useQuery({ queryKey: ["settings"], queryFn: Api.settings });
  const [risk, setRisk] = useState<RiskSettings | null>(null);
  const [acc, setAcc] = useState<Acceptance | null>(null);
  const [bankroll, setBankroll] = useState<number | null>(null);
  useEffect(() => { if (s.data) { setRisk(s.data.risk); setAcc(s.data.acceptance); setBankroll(s.data.paper_bankroll); } }, [s.data]);
  const save = useMutation({
    mutationFn: () => Api.saveSettings({ risk: risk!, acceptance: acc!, paper_bankroll: bankroll ?? undefined }),
    onSuccess: (d) => { qc.setQueryData(["settings"], d); qc.invalidateQueries({ queryKey: ["readiness"] }); },
  });
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
      qc.invalidateQueries({ queryKey: ["readiness"] });
    },
  });
  if (!risk || !acc) return <Panel><Empty>Loading…</Empty></Panel>;
  const dirty = JSON.stringify({ risk, acc, bankroll }) !== JSON.stringify({ risk: s.data!.risk, acc: s.data!.acceptance, bankroll: s.data!.paper_bankroll });
  return (
    <Panel>
      <PanelHeader icon={<Gauge />} title="Risk limits (all strategies)" />
      <div className="space-y-2.5 p-5">
        <NumberField label="Paper bankroll" suffix="$" step={10} value={bankroll} onChange={setBankroll} />
        {RISK_FIELDS.map((f) => <NumberField key={f.k} label={f.label} hint={f.hint} step={f.step} suffix={f.suffix} value={risk[f.k]}
          onChange={(v) => setRisk({ ...risk, [f.k]: v ?? 0 })} />)}
        <div className="panel-title !mt-5 !text-[10.5px]">Live acceptance criteria</div>
        {ACC_FIELDS.map((f) => <NumberField key={f.k} label={f.label} step={f.step} value={acc[f.k]} onChange={(v) => setAcc({ ...acc, [f.k]: v ?? 0 })} />)}
        {save.error && <p className="text-sm text-down" role="alert">{save.error.message}</p>}
        <Button variant="gold" className="mt-3 w-full" disabled={!dirty} loading={save.isPending} onClick={() => save.mutate()}>{save.isSuccess && !dirty ? "Saved ✓" : "Save risk settings"}</Button>

        <div className="panel-title !mt-6 !text-[10.5px]">Paper book management</div>
        <div className="space-y-2 rounded-xl border border-line-mid bg-bg-0/60 p-3.5">
          <div>
            <div className="text-xs font-semibold text-tx-hi">Reset paper book</div>
            <p className="text-[11px] text-tx-low">Clears paper trades, open positions, and resets drawdown back to 0% and fresh starting bankroll.</p>
          </div>
          {resetBook.error && <p className="text-xs text-down">{resetBook.error.message}</p>}
          {resetBook.isSuccess && !confirmReset && <p className="text-xs text-up">Paper book and drawdown reset to 0% ✓</p>}
          {confirmReset ? (
            <div className="flex items-center gap-2 pt-1">
              <span className="text-[11.5px] text-tx-mid">Are you sure?</span>
              <Button size="sm" variant="danger" loading={resetBook.isPending} onClick={() => resetBook.mutate()}>
                Yes, reset book
              </Button>
              <Button size="sm" variant="ghost" onClick={() => setConfirmReset(false)}>
                Cancel
              </Button>
            </div>
          ) : (
            <Button
              size="sm"
              variant="outline"
              className="w-full text-xs hover:border-down/60 hover:text-down"
              onClick={() => setConfirmReset(true)}
            >
              <RotateCcw className="mr-1.5 h-3.5 w-3.5" />
              Reset paper book
            </Button>
          )}
        </div>
      </div>
    </Panel>
  );
}

function EngineNote() {
  const e = useQuery({ queryKey: ["engine"], queryFn: Api.engineStatus, refetchInterval: 10_000 });
  if (!e.data) return null;
  return <span className={cn("ml-auto text-[11px]", e.data.bot_running ? "text-up" : "text-tx-dim")}>
    {e.data.bot_running ? `Bot running (${e.data.mode}): enabled strategies are evaluated every second` : "Bot stopped: press Start bot in the header to run enabled strategies"}
  </span>;
}

const TAB_BADGE: Record<string, string> = { binance_lead: "alpha lead", twap_lock: "final minute", constellation: "reference default", pocket_aces: "sniper", spork_maker: "maker", crypto_strikes: "strike markets", pair_arb: "arbitrage" };

function Strategies() {
  const q = useQuery({ queryKey: ["strategies"], queryFn: Api.strategies });
  const [tab, setTab] = useState("constellation");
  const enabled = q.data?.filter((s) => s.enabled) ?? [];
  return (
    <Panel>
      <PanelHeader icon={<Cog />} title="Strategy configuration" />
      <div className="flex flex-wrap items-center gap-2 border-b border-line bg-up/5 px-5 py-3 text-[12px]">
        <span className="font-num tracking-[0.12em] text-tx-low uppercase">Enabled</span>
        {enabled.length ? enabled.map((s) => <Badge key={s.key} className="border-up/50 text-up">{s.name}</Badge>) : <Badge className="border-line-hi text-tx-low">none</Badge>}
        <EngineNote />
      </div>
      {q.error && <Empty>{q.error.message}</Empty>}
      {q.data && (
        <Tabs.Root value={tab} onValueChange={setTab} className="p-5">
          <Tabs.List className="mb-5 flex gap-2 overflow-x-auto pb-1" aria-label="strategies">
            {q.data.map((s) => (
              <Tabs.Trigger key={s.key} value={s.key}
                className={cn("flex shrink-0 items-center gap-2 rounded-full border px-4 py-2.5 font-num text-xs font-bold tracking-wider uppercase transition-colors",
                  tab === s.key ? "gold-pill border-transparent" : "border-line-mid text-tx-mid hover:text-tx-hi")}>
                {s.enabled && <span className="h-1.5 w-1.5 rounded-full bg-up" />}{s.name}
                {TAB_BADGE[s.key] && <span className={cn("rounded-full border px-1.5 py-px text-[9px]", tab === s.key ? "border-[#2a1a06]/30" : "border-line-hi")}>{TAB_BADGE[s.key]}</span>}
              </Tabs.Trigger>
            ))}
          </Tabs.List>
          {q.data.map((s) => <Tabs.Content key={s.key} value={s.key}><StrategyEditor strategy={s} /></Tabs.Content>)}
        </Tabs.Root>
      )}
    </Panel>
  );
}

export default function Settings() {
  const loc = useLocation();
  useEffect(() => { if (loc.hash) document.querySelector(loc.hash)?.scrollIntoView({ behavior: "smooth" }); }, [loc.hash]);
  const health = useQuery({ queryKey: ["health"], queryFn: Api.health, refetchInterval: 30_000 });
  return (
    <div className="grid grid-cols-[minmax(0,1fr)] gap-5 xl:grid-cols-[420px_minmax(0,1fr)]">
      <div className="space-y-5">
        <WalletPanel />
        <LiveTrading />
        <RiskPanel />
        <Panel>
          <PanelHeader icon={<Server />} title="Server status" />
          <ul className="space-y-1.5 p-5 text-[13px]">
            <li className="flex justify-between"><span>Database</span><span className={health.data?.db ? "text-up" : "text-down"}>{health.data?.db ? "ok" : "down"}</span></li>
            <li className="flex justify-between"><span>Redis</span><span className={health.data?.redis ? "text-up" : "text-down"}>{health.data?.redis ? "ok" : "down"}</span></li>
            {Object.values(health.data?.feeds ?? {}).map((f) => (
              <li key={f.name}>
                <div className="flex justify-between"><span className="capitalize">{f.name === "clob" ? "Polymarket books" : f.name === "registry" ? "Market discovery" : f.name === "writer" ? "History writer" : "Binance prices"}</span>
                  <span className={f.status === "live" ? "text-up" : f.status === "stale" || f.status === "connecting" ? "text-warn" : "text-down"}>{f.status}{f.reconnects ? ` · ${f.reconnects} reconnects` : ""}</span></div>
                {f.status !== "live" && f.last_error && <div className="mt-0.5 text-[11px] break-words text-tx-low">{f.last_error}</div>}
              </li>
            ))}
            {health.data?.config_problems.map((p) => <li key={p} className="text-warn">⚠ {p}</li>)}
          </ul>
        </Panel>
      </div>
      <Strategies />
    </div>
  );
}
