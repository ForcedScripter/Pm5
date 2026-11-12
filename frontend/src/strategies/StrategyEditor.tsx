import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AnimatePresence, motion } from "framer-motion";
import { Check, RotateCcw } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Button } from "@/components/ui/button";
import { Chips, FieldGroup, NumberField, Segmented, Switch } from "@/components/ui/form";
import { ASSETS, ASSET_META, cn } from "@/lib/utils";
import { Api } from "@/services/api";
import type { Strategy, StrategyKey } from "@/types/api";

import { DCA_OPTIONS, DCA_PRESETS, MODE_OPTIONS, MODE_PRESETS, VOL_BY_HOUR } from "./presets";
import { Scorecard } from "./Scorecard";
import { SignalTable } from "./SignalTable";

type P = Record<string, any>; // eslint-disable-line @typescript-eslint/no-explicit-any

const get = (o: P, path: string): any => path.split(".").reduce<any>((x, k) => x?.[k], o); // eslint-disable-line @typescript-eslint/no-explicit-any
function set(o: P, path: string, v: unknown): P {
  const keys = path.split(".");
  const out = structuredClone(o);
  let cur = out;
  keys.slice(0, -1).forEach((k) => { cur[k] = cur[k] ?? {}; cur = cur[k]; });
  cur[keys.at(-1)!] = v;
  return out;
}

interface F { path: string; label: string; hint?: string; step?: number; suffix?: string }

const SPECIFIC: Record<StrategyKey, { title: string; fields: F[] }> = {
  constellation: { title: "Consensus & laggard", fields: [
    { path: "min_consensus", label: "Min assets agreeing", hint: "How many of the six coins must move the same way.", step: 1 },
    { path: "max_laggard_delta_pct", label: "Laggard gap", hint: "The straggler may have moved at most this % (abs) since the round opened.", step: 0.01, suffix: "%" },
    { path: "min_spot_delta_pct", label: "Min move to count", hint: "A coin counts as moving once its round move passes this %.", step: 0.005, suffix: "%" },
    { path: "up_threshold", label: "UP threshold", hint: "Average move of the agreeing group needed for UP.", step: 0.005, suffix: "%" },
    { path: "down_threshold", label: "DOWN threshold", hint: "Average move of the agreeing group needed for DOWN.", step: 0.005, suffix: "%" },
    { path: "min_fair_edge", label: "Min edge vs fair value", hint: "Fair value minus ask minus fee must be at least this. In the recorded rounds the laggard only paid when its token was under about 50¢; this keeps entries there. Empty = off.", step: 0.005, suffix: "$" },
  ] },
  twap_lock: { title: "Final-minute lock", fields: [
    { path: "min_sigmas", label: "Lock strength", hint: "The rest of the closing minute would need to move at least this many standard deviations to flip the result.", step: 0.25, suffix: "σ" },
    { path: "min_locked_seconds", label: "Seconds already averaged", hint: "How much of the 60-second closing average must be fixed before entering.", step: 5, suffix: "s" },
    { path: "min_edge", label: "Min edge after fee", hint: "Model probability minus ask minus the taker fee per share.", step: 0.005, suffix: "$" },
    { path: "max_edge", label: "Max edge", hint: "A bigger gap usually means the model is wrong, not the market.", step: 0.01, suffix: "$" },
    { path: "min_seconds_before_close", label: "Latest entry before close", hint: "Lowers the risk engine's 'too close to resolution' limit for this strategy only. Never under 5s.", step: 1, suffix: "s" },
    { path: "stake_usd", label: "Stake per trade", step: 1, suffix: "$" },
    { path: "slippage", label: "Slippage allowance", step: 0.005 },
  ] },
  binance_lead: { title: "Binance Lead-Lag Alpha (ce25)", fields: [
    { path: "lead_threshold_bps", label: "Lead trigger", hint: "Spot lead over the round's open, in basis points. Below ~12 bps Binance and Chainlink (the settlement source) can disagree.", step: 0.5, suffix: "bps" },
    { path: "confirm_seconds", label: "Confirmation wait", hint: "Seconds the lead must hold, to reject 1-second spikes.", step: 1, suffix: "s" },
    { path: "max_offset_seconds", label: "Lead must start by", hint: "Only early leads count: the lag edge is gone later in the round.", step: 10, suffix: "s" },
    { path: "min_entry_price", label: "Min price", hint: "Floor, to avoid near-dead tokens.", step: 0.01, suffix: "$" },
    { path: "max_entry_price", label: "Max price", hint: "Only buy while Polymarket still lags the move.", step: 0.01, suffix: "$" },
    { path: "min_edge", label: "Min edge", hint: "Fair value minus ask must be at least this.", step: 0.01, suffix: "$" },
    { path: "max_edge", label: "Max edge", hint: "A bigger 'edge' usually means the model is wrong, not the market.", step: 0.01, suffix: "$" },
    { path: "stake_usd", label: "Stake per trade", step: 1, suffix: "$" },
    { path: "slippage", label: "Slippage allowance", step: 0.005 },
  ] },
  custom_btc: { title: "Directional rules", fields: [
    { path: "up_threshold", label: "UP threshold", hint: "Round move % that triggers an UP entry.", step: 0.005, suffix: "%" },
    { path: "down_threshold", label: "DOWN threshold", step: 0.005, suffix: "%" },
    { path: "min_confidence", label: "Min confidence", step: 0.05 },
    { path: "cooldown_s", label: "Cooldown", step: 10, suffix: "s" },
    { path: "max_trades_per_window", label: "Max trades / round", step: 1 },
    { path: "max_exposure_usd", label: "Max exposure", step: 1, suffix: "$" },
  ] },
  odds_scalper: { title: "Entry: wait for the multiplier to peak, then reverse", fields: [
    { path: "min_multiplier", label: "Min multiplier", hint: "Qualify when a side pays at least this much (3.0x = price ≤ 33.3¢).", step: 0.1, suffix: "x" },
    { path: "max_multiplier", label: "Max multiplier", hint: "Ignore near-dead tokens above this (8x = below 12.5¢).", step: 0.5, suffix: "x" },
    { path: "qualify_window_s", label: "Detect within", hint: "Only look for qualifying sides in the first N seconds of the round.", step: 5, suffix: "s" },
    { path: "max_observation_s", label: "Extra entry time", hint: "Entries allowed until the detect window + this.", step: 1, suffix: "s" },
    { path: "observe_min_s", label: "Observe at least", hint: "Never buy sooner than this after the first qualifying price.", step: 1, suffix: "s" },
    { path: "reversal_cents", label: "Reversal size", hint: "The ask must rise this many cents off its low (the multiplier peak).", step: 1, suffix: "¢" },
    { path: "confirm_s", label: "Reversal must hold", hint: "…and stay up this long: no buying on one noisy print.", step: 0.5, suffix: "s" },
    { path: "max_entry_spread", label: "Max entry spread", step: 0.005 },
    { path: "min_depth_usd", label: "Min depth at ask", step: 5, suffix: "$" },
    { path: "max_required_move_cents", label: "Max bounce needed", hint: "Skip if fees on both sides + the profit target need a bigger move than this.", step: 0.5, suffix: "¢" },
    { path: "stake_usd", label: "Stake per trade", step: 1, suffix: "$" },
    { path: "slippage", label: "Slippage allowance", step: 0.005 },
  ] },
  pocket_aces: { title: "Fair-value & retrace filters", fields: [
    { path: "up_threshold", label: "UP threshold", hint: "Round move % that triggers an UP entry.", step: 0.005, suffix: "%" },
    { path: "down_threshold", label: "DOWN threshold", step: 0.005, suffix: "%" },
    { path: "min_edge", label: "Min edge", hint: "Fair value minus ask must be at least this.", step: 0.01, suffix: "$" },
    { path: "max_edge", label: "Max edge", hint: "A bigger 'edge' usually means the model is wrong, not the market.", step: 0.01, suffix: "$" },
    { path: "min_peak_gap_ratio", label: "Min lead vs its peak", hint: "Skip when the lead has retraced below this share of its peak this round.", step: 0.05 },
    { path: "min_gap_safety_atr", label: "Min distance from strike", hint: "Spot must be at least this many ATRs from the strike.", step: 0.1 },
  ] },
  pair_arb: { title: "Pair arbitrage", fields: [] },
  crypto_strikes: { title: "Model & bids", fields: [
    { path: "min_edge", label: "Bid this far under the model value", hint: "The resting bid sits at model value minus this, never at or through the ask.", step: 0.005, suffix: "$" },
    { path: "max_edge", label: "Max disagreement with the market", hint: "A larger gap is treated as the model being wrong, and not traded.", step: 0.01, suffix: "$" },
    { path: "exit_edge", label: "Sell when the bid beats the model by", hint: "After the taker fee. Otherwise the position is held to settlement.", step: 0.005, suffix: "$" },
    { path: "min_prob", label: "Lowest model probability traded", hint: "Far strikes are where a log-normal model is weakest.", step: 0.01 },
    { path: "max_prob", label: "Highest model probability traded", step: 0.01 },
    { path: "vol_mult", label: "Volatility multiplier", hint: "Scales the blended 1-day / 7-day / 30-day realised volatility. 1 = as measured.", step: 0.05 },
    { path: "spot_max_age_s", label: "Max spot age", step: 1, suffix: "s" },
  ] },
  spork_maker: { title: "Quoting (spork30)", fields: [
    { path: "fair_edge", label: "Edge over fair", hint: "Each sell rests at fair value + this, rounded up to the tick.", step: 0.005, suffix: "$" },
    { path: "trend_edge_bump", label: "Edge after one-sided losses", hint: "Extra edge per recent losing round where only one side filled (max 3).", step: 0.005, suffix: "$" },
    { path: "fair_edge_vol_mult", label: "Volatility-scaled edge", hint: "Edge at least this × σ·√(seconds left). 0 = off.", step: 0.1 },
    { path: "max_lean_shares", label: "Max lean", hint: "Pull a side once it has sold this many shares more than the other.", step: 1, suffix: "sh" },
    { path: "queue_ahead", label: "Modelled queue ahead", hint: "Paper fill model: shares that must trade at our price before we fill. Higher = more pessimistic.", step: 50, suffix: "sh" },
    { path: "quote_pause_final_s", label: "Pull quotes in final", hint: "0 = quote until the close.", step: 5, suffix: "s" },
    { path: "spot_max_age_s", label: "Max spot age", hint: "Pull quotes when Binance spot is older than this.", step: 0.5, suffix: "s" },
  ] },
};
const ARB_FIELDS: F[] = [
  { path: "min_profit", label: "Min locked profit per pair", hint: "Up ask + Down ask + both taker fees must be at most $1 minus this.", step: 0.001, suffix: "$" },
  { path: "max_pair_usd", label: "Max per purchase", hint: "Also limited by your per-trade risk limit.", step: 1, suffix: "$" },
  { path: "max_round_usd", label: "Max per round", step: 5, suffix: "$" },
  { path: "min_shares", label: "Min pairs", hint: "Skip if fewer pairs than this are available under $1.", step: 1, suffix: "sh" },
  { path: "persist_ms", label: "Must persist", hint: "The gap has to still be there this long after it appears, the time a real order needs to arrive.", step: 50, suffix: "ms" },
  { path: "max_book_age_s", label: "Max book age", step: 0.25, suffix: "s" },
  { path: "min_seconds_left", label: "No buys in the last", step: 5, suffix: "s" },
];
const STRIKE_LIMITS: F[] = [
  { path: "stake_usd", label: "Stake per bid", step: 1, suffix: "$" },
  { path: "max_open_positions", label: "Max open positions", step: 1 },
  { path: "max_event_usd", label: "Max per event", hint: "Strikes of one event (same day or week) move together.", step: 5, suffix: "$" },
  { path: "max_total_usd", label: "Max for all strike markets", hint: "Cap for the whole category.", step: 5, suffix: "$" },
  { path: "min_hours_left", label: "No new bids in the last", step: 0.25, suffix: "h" },
  { path: "max_days_left", label: "Only markets expiring within", step: 0.5, suffix: "d" },
  { path: "max_spread", label: "Max spread", step: 0.005 },
  { path: "poll_s", label: "Check the books every", step: 5, suffix: "s" },
];
const MAKER_SIZING: F[] = [
  { path: "max_sets", label: "Max sets per round", hint: "Hard cap. 1 set = $1 = 1 UP + 1 DOWN.", step: 10 },
  { path: "min_sets", label: "Min sets", hint: "Skip the round if conditions allow fewer.", step: 5 },
  { path: "bankroll_frac", label: "Max share of cash", hint: "Never lock more than this fraction of paper cash in one round.", step: 0.01 },
  { path: "reward_bankroll_frac", label: "Reward-size round: max share of cash", hint: "If this much cash covers the market's liquidity-reward minimum (50 shares on BTC), run one round at that size. 0 = off.", step: 0.05 },
  { path: "volume_frac", label: "Max × recent volume", hint: "Sets ≤ this × shares traded on the market in the lookback.", step: 0.05 },
  { path: "volume_lookback_s", label: "Volume lookback", step: 60, suffix: "s" },
  { path: "min_print_volume", label: "Min recent volume", hint: "Shares traded (both sides) in the lookback, to be eligible.", step: 50, suffix: "sh" },
  { path: "max_spread", label: "Max spread", step: 0.005 },
  { path: "max_annual_vol", label: "Max volatility", hint: "Annualised. High-vol rounds trend, and trend is where makers get picked off.", step: 0.1 },
  { path: "clip_divisor", label: "Clip = sets ÷", hint: "Size of each resting sell, between 5 shares (Polymarket minimum) and the max clip.", step: 1 },
  { path: "max_clip", label: "Max clip", step: 5, suffix: "sh" },
];
const SCALPER_EXITS: F[] = [
  { path: "min_net_profit_usd", label: "Min net profit", hint: "After both taker fees, on the actual bid.", step: 0.05, suffix: "$" },
  { path: "arm_hold_s", label: "Profit must persist", hint: "The position must stay above the min profit this long before trailing starts.", step: 1, suffix: "s" },
  { path: "trail_cents", label: "Sell on a dip of", hint: "Once armed, sell when the bid falls this much from its high.", step: 1, suffix: "¢" },
  { path: "max_hold_s", label: "Max holding time", step: 5, suffix: "s" },
  { path: "stop_loss_cents", label: "Stop-loss", hint: "Sell if the bid falls this far below the entry price.", step: 1, suffix: "¢" },
  { path: "min_hold_before_adverse_s", label: "Breathing room", hint: "Seconds position must be held before adverse momentum selling is allowed.", step: 1, suffix: "s" },
  { path: "adverse_drop_cents", label: "Adverse drop", hint: "Sell a losing position if the bid falls this much…", step: 1, suffix: "¢" },
  { path: "adverse_window_s", label: "…within", step: 1, suffix: "s" },
  { path: "max_straddle_cost", label: "Max straddle cost", hint: "If opposite side drops so combined cost <= this, buy opposite side to lock in guaranteed $1 payout.", step: 0.02, suffix: "$" },
  { path: "min_exit_depth_ratio", label: "Min bid size (× shares)", hint: "Take a profitable exit early if buyers at the bid thin out below this.", step: 0.25 },
];
const HAS_MODE: StrategyKey[] = ["constellation", "custom_btc"];
const ASSET_PICK: StrategyKey[] = ["binance_lead", "twap_lock", "constellation", "pocket_aces", "spork_maker", "pair_arb"];

export function StrategyEditor({ strategy }: { strategy: Strategy }) {
  const qc = useQueryClient();
  const [draft, setDraft] = useState<P>(strategy.params);
  useEffect(() => setDraft(strategy.params), [strategy.params, strategy.key]);
  const dirty = useMemo(() => JSON.stringify(draft) !== JSON.stringify(strategy.params), [draft, strategy.params]);
  const upd = (path: string, v: unknown) => setDraft((d) => set(d, path, v));
  const done = (s: Strategy) => qc.setQueryData<Strategy[]>(["strategies"], (all) => all?.map((x) => (x.key === s.key ? s : x)));

  const save = useMutation({ mutationFn: () => Api.saveStrategy(strategy.key, draft), onSuccess: done });
  const reset = useMutation({ mutationFn: () => Api.resetStrategy(strategy.key), onSuccess: done });
  const toggle = useMutation({ mutationFn: (on: boolean) => Api.setStrategyEnabled(strategy.key, on), onSuccess: done });

  const k = strategy.key;
  const locked = false;
  const scalper = k === "odds_scalper";
  const maker = k === "spork_maker";
  const strikes = k === "crypto_strikes";
  const arb = k === "pair_arb";
  const field = (f: F) => <NumberField key={f.path} label={f.label} hint={f.hint} step={f.step} suffix={f.suffix}
    value={get(draft, f.path)} onChange={(v) => upd(f.path, v)} />;

  return (
    <div className="space-y-6">
      <div className="glass glass-gold flex flex-wrap items-start justify-between gap-4 p-5">
        <div className="max-w-2xl">
          <h3 className="font-brand text-2xl text-amber-hi">{strategy.name}</h3>
          <p className="mt-1.5 text-[13px] leading-relaxed text-tx">{strategy.description}</p>
          <p className="mt-2 font-num text-[11px] text-tx-dim">config v{strategy.version}</p>
          {strategy.edge_decay?.paused && (
            <p className="mt-2 rounded-md border border-down/40 bg-down/10 px-2.5 py-1.5 text-[12px] text-down">
              Live entries paused: mean P&amp;L ${strategy.edge_decay.mean_pnl?.toFixed(2)} over its last {strategy.edge_decay.n} trades. Paper keeps running; live resumes once the rolling mean is back above $0.
            </p>
          )}
        </div>
        <label className="flex items-center gap-3 rounded-full border border-line-mid bg-bg-0/50 px-4 py-2">
          <span className={cn("font-num text-xs font-bold tracking-wider", strategy.enabled ? "text-up" : "text-tx-low")}>{strategy.enabled ? "ENABLED" : "DISABLED"}</span>
          <Switch checked={strategy.enabled} disabled={toggle.isPending} onCheckedChange={(v) => toggle.mutate(v)} aria-label={`enable ${strategy.name}`} />
        </label>
      </div>

      <Scorecard strategy={k} />

      {scalper && (
        <div className="grid gap-6 lg:grid-cols-2">
          <FieldGroup title={SPECIFIC[k].title}><div className="space-y-2.5">{SPECIFIC[k].fields.map(field)}</div></FieldGroup>
          <FieldGroup title="Exit: trail the bounce, out within the max hold"><div className="space-y-2.5">{SCALPER_EXITS.map(field)}</div></FieldGroup>
        </div>
      )}
      {scalper && (
        <FieldGroup title="Markets">
          <Chips values={draft.assets ?? []} options={ASSETS.map((a) => ({ value: a, label: `${ASSET_META[a].glyph} ${ASSET_META[a].sym}` }))}
            onToggle={(a) => { const s2 = new Set<string>(draft.assets); if (s2.has(a)) s2.delete(a); else s2.add(a); if (s2.size) upd("assets", ASSETS.filter((x) => s2.has(x))); }} />
        </FieldGroup>
      )}
      {maker && (
        <>
          <p className="rounded-md border border-amber/30 bg-amber/5 px-3 py-2 text-[12px] leading-relaxed text-tx">
            Paper only. Fills come from real Polymarket trade prints, after a modelled queue of {String(draft.queue_ahead ?? "")} shares ahead of each sell. Queue position is assumed, not measured, and real maker fills are usually worse. Check the one-sided share and markouts in History → Maker rounds before trusting the P&amp;L.
          </p>
          <div className="grid gap-6 lg:grid-cols-2">
            <FieldGroup title={SPECIFIC[k].title}><div className="space-y-2.5">{SPECIFIC[k].fields.map(field)}</div></FieldGroup>
            <FieldGroup title="Adaptive size & eligibility"><div className="space-y-2.5">{MAKER_SIZING.map(field)}</div></FieldGroup>
          </div>
          <FieldGroup title="Markets" hint="Only the ones that pass the eligibility checks each round are quoted.">
            <Chips values={draft.assets ?? []} options={ASSETS.map((a) => ({ value: a, label: `${ASSET_META[a].glyph} ${ASSET_META[a].sym}` }))}
              onToggle={(a) => { const s2 = new Set<string>(draft.assets); if (s2.has(a)) s2.delete(a); else s2.add(a); if (s2.size) upd("assets", ASSETS.filter((x) => s2.has(x))); }} />
          </FieldGroup>
        </>
      )}
      {strikes && (
        <>
          <p className="rounded-md border border-amber/30 bg-amber/5 px-3 py-2 text-[12px] leading-relaxed text-tx">
            Paper only. Trades BTC and ETH "above $X at noon ET" and "reach / dip to $X" markets, which settle on Binance 1-minute candles. A resting bid counts as filled only when the real order book trades through it. Judge it on History: the model must beat the market's own prices there before the P&amp;L means anything.
          </p>
          <div className="grid gap-6 lg:grid-cols-2">
            <FieldGroup title={SPECIFIC[k].title}><div className="space-y-2.5">{SPECIFIC[k].fields.map(field)}</div></FieldGroup>
            <FieldGroup title="Size & limits"><div className="space-y-2.5">{STRIKE_LIMITS.map(field)}</div></FieldGroup>
          </div>
          <FieldGroup title="Markets">
            <Chips values={draft.assets ?? []} options={(["btc", "eth"] as const).map((a) => ({ value: a, label: `${ASSET_META[a].glyph} ${ASSET_META[a].sym}` }))}
              onToggle={(a) => { const s2 = new Set<string>(draft.assets); if (s2.has(a)) s2.delete(a); else s2.add(a); if (s2.size) upd("assets", ["btc", "eth"].filter((x) => s2.has(x))); }} />
            <div className="mt-3">
              <Chips values={draft.kinds ?? []} options={[{ value: "above", label: "Above at noon" }, { value: "reach", label: "Reach" }, { value: "dip", label: "Dip" }]}
                onToggle={(a) => { const s2 = new Set<string>(draft.kinds); if (s2.has(a)) s2.delete(a); else s2.add(a); if (s2.size) upd("kinds", ["above", "dip", "reach"].filter((x) => s2.has(x))); }} />
            </div>
          </FieldGroup>
        </>
      )}
      {arb && (
        <>
          <p className="rounded-md border border-amber/30 bg-amber/5 px-3 py-2 text-[12px] leading-relaxed text-tx">
            Paper only. Buys Up and Down in the same round when both asks plus both fees cost under $1, which locks the difference whoever wins. It is rare: in this bot's recorded books it appeared in 9 of 1,321 rounds, about 1.5¢ a pair. Live trading is refused because one side can fill without the other.
          </p>
          <FieldGroup title="Pair arbitrage"><div className="space-y-2.5">{ARB_FIELDS.map(field)}</div></FieldGroup>
          <FieldGroup title="Markets">
            <Chips values={draft.assets ?? []} options={ASSETS.map((a) => ({ value: a, label: `${ASSET_META[a].glyph} ${ASSET_META[a].sym}` }))}
              onToggle={(a) => { const s2 = new Set<string>(draft.assets); if (s2.has(a)) s2.delete(a); else s2.add(a); if (s2.size) upd("assets", ASSETS.filter((x) => s2.has(x))); }} />
          </FieldGroup>
        </>
      )}
      {!scalper && !maker && !strikes && !arb && <>
      {HAS_MODE.includes(k) && (
        <FieldGroup title="Consensus strictness" hint="Presets fill the entry gates below; you can still edit them.">
          <Segmented<keyof typeof MODE_PRESETS> value={draft.mode} options={MODE_OPTIONS.map((o) => ({ ...o }))}
            onChange={(m) => setDraft((d) => ({ ...d, mode: m, ...MODE_PRESETS[m] }))} />
        </FieldGroup>
      )}

      <div className="grid gap-6 lg:grid-cols-2">
        {!locked && SPECIFIC[k].fields.length > 0 && (
          <FieldGroup title={SPECIFIC[k].title}><div className="space-y-2.5">{SPECIFIC[k].fields.map(field)}</div></FieldGroup>
        )}
        <FieldGroup title="Amounts & risk">
          <div className="space-y-2.5">
            <NumberField label="Initial buy" suffix="$" step={0.5} value={draft.dca?.stages?.[0]}
              onChange={(v) => upd("dca.stages", [v ?? 0, ...(draft.dca?.stages ?? []).slice(1)])} />
            <NumberField label="Max per position" suffix="$" step={0.5} value={draft.dca?.max_total} onChange={(v) => upd("dca.max_total", v)} />
            <NumberField label="Max concurrent" step={1} value={draft.max_concurrent} onChange={(v) => upd("max_concurrent", v)} />
            {!locked && <NumberField label="Stop-loss price" hint="Sell if the token bid falls to this; never more than the floor gap below it." step={0.01} value={draft.exits?.stop_loss} onChange={(v) => upd("exits.stop_loss", v)} />}
            {!locked && <NumberField label="Take-profit price" hint="Optional: sell once the bid reaches this. Empty = hold to resolution." step={0.01} value={draft.exits?.take_profit} onChange={(v) => upd("exits.take_profit", v)} />}
            {!locked && (
              <>
                <label className="flex items-center justify-between text-[13px] pt-1">
                  <div>
                    <div className="font-medium text-foreground">Strike-crossing stop</div>
                    <div className="text-[11px] text-muted">Exit late if spot stays across the strike against the position. Off by default: the evidence for it was later refuted.</div>
                  </div>
                  <Switch checked={draft.exits?.strike_crossing_stop ?? false} onCheckedChange={(v) => upd("exits.strike_crossing_stop", v)} />
                </label>
                {draft.exits?.strike_crossing_stop && (
                  <div className="grid grid-cols-2 gap-2 pt-1">
                    <NumberField label="Exit window" hint="Only check when secs left ≤ this." suffix="s" step={5} value={draft.exits?.strike_crossing_s ?? 60} onChange={(v) => upd("exits.strike_crossing_s", v)} />
                    <NumberField label="Breach buffer" hint="Decisive breach in basis points." suffix="bps" step={1} value={draft.exits?.strike_crossing_buffer_bps ?? 5.0} onChange={(v) => upd("exits.strike_crossing_buffer_bps", v)} />
                    <NumberField label="Must persist" hint="The breach has to last this long (a first-touch stop whipsaws winners)." suffix="s" step={1} value={draft.exits?.strike_crossing_confirm_s ?? 10} onChange={(v) => upd("exits.strike_crossing_confirm_s", v)} />
                  </div>
                )}
              </>
            )}
            <NumberField label="Max spread" step={0.005} value={draft.max_spread} onChange={(v) => upd("max_spread", v)} />
            <NumberField label="Min book depth" suffix="$" step={5} value={draft.min_depth_usd} onChange={(v) => upd("min_depth_usd", v)} />
          </div>
        </FieldGroup>
      </div>

      {!locked && (
        <div className="grid gap-6 lg:grid-cols-2">
          <FieldGroup title="Buy price range" hint="Entry only while the favored token sits in this band (price ≈ implied probability).">
            <div className="space-y-2.5">
              <NumberField label="Band low" step={0.01} value={draft.entry_low} onChange={(v) => upd("entry_low", v)} />
              <NumberField label="Band high" step={0.01} value={draft.entry_high} onChange={(v) => upd("entry_high", v)} />
            </div>
          </FieldGroup>
          <FieldGroup title="Avoid price range (optional)" hint="Entries inside this band are skipped. Leave both at 0 to disable.">
            <div className="space-y-2.5">
              <NumberField label="Avoid low" step={0.01} value={draft.avoid_low} onChange={(v) => upd("avoid_low", v)} />
              <NumberField label="Avoid high" step={0.01} value={draft.avoid_high} onChange={(v) => upd("avoid_high", v)} />
            </div>
          </FieldGroup>
        </div>
      )}

      {ASSET_PICK.includes(k) && (
        <FieldGroup title="Markets">
          <Chips values={draft.assets ?? []} options={ASSETS.map((a) => ({ value: a, label: `${ASSET_META[a].glyph} ${ASSET_META[a].sym}` }))}
            onToggle={(a) => { const s = new Set<string>(draft.assets); if (s.has(a)) s.delete(a); else s.add(a); if (s.size) upd("assets", ASSETS.filter((x) => s.has(x))); }} />
        </FieldGroup>
      )}

      {!locked && (
        <FieldGroup title="Trade direction" hint="Switch one off to stop those setups entirely.">
          <div className="flex gap-6">
            {(["trade_up", "trade_down"] as const).map((d) => (
              <label key={d} className="flex items-center gap-2.5 text-[13px]"><Switch checked={!!draft[d]} onCheckedChange={(v) => upd(d, v)} />
                <span className={d === "trade_up" ? "font-semibold text-up" : "font-semibold text-down"}>{d === "trade_up" ? "▲ UP" : "▼ DOWN"}</span></label>
            ))}
          </div>
        </FieldGroup>
      )}

      {!locked && (
        <FieldGroup title="DCA behavior (averaging down after entry)" hint="In a 5-minute binary a sliding price usually means the market turned against you. Off or On confirm are the safer choices.">
          <Segmented<keyof typeof DCA_PRESETS> cols={4} value={draft.dca?.mode} options={DCA_OPTIONS.map((o) => ({ ...o }))}
            onChange={(m) => setDraft((d) => set(set(set(d, "dca.mode", m), "dca.stages", [...DCA_PRESETS[m].stages]), "dca.max_total", DCA_PRESETS[m].max_total))} />
          <div className="grid gap-2.5 pt-2 lg:grid-cols-2">
            {(draft.dca?.stages ?? []).slice(1).map((amt: number, i: number) => (
              <NumberField key={i} label={`Top-up ${i + 1}`} suffix="$" step={0.5} value={amt}
                onChange={(v) => { const st = [...draft.dca.stages]; st[i + 1] = v ?? 0; upd("dca.stages", st); }} />
            ))}
            <NumberField label="Drop per step" hint="Token price fall from the first entry that triggers each top-up." step={0.01} value={draft.dca?.step_drop} onChange={(v) => upd("dca.step_drop", v)} />
            <NumberField label="Max average price" step={0.01} value={draft.dca?.max_avg_price} onChange={(v) => upd("dca.max_avg_price", v)} />
            <NumberField label="Min gap between buys" suffix="s" step={5} value={draft.dca?.min_seconds_between} onChange={(v) => upd("dca.min_seconds_between", v)} />
            <NumberField label="Halt before close" suffix="s" step={10} value={draft.dca?.halt_seconds_before_close} onChange={(v) => upd("dca.halt_seconds_before_close", v)} />
          </div>
        </FieldGroup>
      )}

      {!locked && (
        <FieldGroup title="Timing">
          <div className="grid gap-2.5 lg:grid-cols-2">
            <NumberField label="First check" hint="Seconds into the round before scanning starts." suffix="s" step={5} value={draft.window?.first_check_s} onChange={(v) => upd("window.first_check_s", v)} />
            <NumberField label="Last entry" hint="No new entries after this many seconds." suffix="s" step={5} value={draft.window?.last_entry_s} onChange={(v) => upd("window.last_entry_s", v)} />
            {"streak_reversal" in draft && (<>
              <label className="flex items-center justify-between text-[13px]">Streak reversal<Switch checked={!!draft.streak_reversal} onCheckedChange={(v) => upd("streak_reversal", v)} /></label>
              <NumberField label="Streak length" step={1} value={draft.streak_length} onChange={(v) => upd("streak_length", v)} />
            </>)}
          </div>
        </FieldGroup>
      )}

      {!locked && (
        <FieldGroup title="Skip hours (UTC)" hint="No new entries in the selected hours. Bar height = BTC volatility by hour.">
          <div className="grid grid-cols-12 gap-1.5">
            {VOL_BY_HOUR.map((vol, h) => {
              const on = (draft.window?.skip_hours_utc ?? []).includes(h);
              return (
                <button key={h} type="button" aria-pressed={on}
                  onClick={() => { const s = new Set<number>(draft.window?.skip_hours_utc ?? []); if (on) s.delete(h); else s.add(h); upd("window.skip_hours_utc", [...s].sort((a, b) => a - b)); }}
                  className={cn("relative h-12 overflow-hidden rounded-lg border font-num text-[11px] font-semibold", on ? "border-down/60 text-down" : "border-line-mid text-tx-low hover:text-tx")}>
                  <span className={cn("absolute inset-x-0 bottom-0", on ? "bg-down/25" : "bg-amber/15")} style={{ height: `${Math.max(8, vol)}%` }} />
                  <span className="relative">{String(h).padStart(2, "0")}</span>
                </button>
              );
            })}
          </div>
        </FieldGroup>
      )}

      </>}

      <FieldGroup title="Recent signals">
        <div className="glass overflow-hidden"><SignalTable strategy={k} compact /></div>
      </FieldGroup>

      <div className="sticky bottom-3 z-10 flex flex-wrap items-center gap-3 rounded-full border border-line-hi bg-bg-0/90 p-2 pl-5 backdrop-blur">
        <span className="text-[12px] text-tx-low">{dirty ? "Unsaved changes" : "Saved"}</span>
        <AnimatePresence>{save.isSuccess && !dirty && <motion.span initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} className="flex items-center gap-1 text-xs text-up"><Check className="h-3.5 w-3.5" />v{strategy.version} saved</motion.span>}</AnimatePresence>
        {(save.error || reset.error || toggle.error) && <span className="text-xs text-down" role="alert">{(save.error ?? reset.error ?? toggle.error)!.message}</span>}
        <div className="ml-auto flex gap-2">
          <Button variant="ghost" size="sm" onClick={() => reset.mutate()} loading={reset.isPending}><RotateCcw className="h-3.5 w-3.5" />Reset defaults</Button>
          <Button variant="outline" size="sm" disabled={!dirty} onClick={() => setDraft(strategy.params)}>Discard</Button>
          <Button variant="gold" size="sm" disabled={!dirty} loading={save.isPending} onClick={() => save.mutate()}>Save strategy</Button>
        </div>
      </div>
    </div>
  );
}
