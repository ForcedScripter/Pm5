import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AnimatePresence, motion } from "framer-motion";
import { Check, RotateCcw } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Button } from "@/components/ui/button";
import { Chips, FieldGroup, NumberField, Segmented, Switch } from "@/components/ui/form";
import { ASSETS, ASSET_META, cn } from "@/lib/utils";
import { Api } from "@/services/api";
import type { Strategy, StrategyKey } from "@/types/api";
import { AiModelPanel } from "./AiModelPanel";
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
  ] },
  ai_strat: { title: "Model gate", fields: [
    { path: "min_probability", label: "Min win probability", hint: "Calibrated P(win) the model must output.", step: 0.01 },
    { path: "min_edge", label: "Min edge over price", hint: "P(win) − token price must exceed this.", step: 0.005 },
    { path: "min_training_signals", label: "Min training signals", hint: "The model stays off until this many resolved signals exist.", step: 50 },
  ] },
  mean_reversion: { title: "Regime & reversion", fields: [
    { path: "regime_1h_pct", label: "Regime trigger (1h)", hint: "BTC 1h change that flips the regime.", step: 0.1, suffix: "%" },
    { path: "regime_down_threshold", label: "DOWN threshold when hot", step: 0.005, suffix: "%" },
    { path: "regime_up_threshold", label: "UP threshold when cold", step: 0.005, suffix: "%" },
    { path: "lookback_s", label: "Lookback", step: 30, suffix: "s" },
    { path: "zscore_entry", label: "Z-score entry", step: 0.1 },
    { path: "vol_min_pct", label: "Min volatility", step: 0.01, suffix: "%" },
    { path: "vol_max_pct", label: "Max volatility", step: 0.01, suffix: "%" },
  ] },
  custom_btc: { title: "Directional rules", fields: [
    { path: "up_threshold", label: "UP threshold", hint: "Round move % that triggers an UP entry.", step: 0.005, suffix: "%" },
    { path: "down_threshold", label: "DOWN threshold", step: 0.005, suffix: "%" },
    { path: "min_confidence", label: "Min confidence", step: 0.05 },
    { path: "cooldown_s", label: "Cooldown", step: 10, suffix: "s" },
    { path: "max_trades_per_window", label: "Max trades / round", step: 1 },
    { path: "max_exposure_usd", label: "Max exposure", step: 1, suffix: "$" },
  ] },
  custom_eth: { title: "Directional rules", fields: [] },
  pocket_aces: { title: "Sizing", fields: [] },
};
SPECIFIC.custom_eth.fields = SPECIFIC.custom_btc.fields;

const HAS_MODE: StrategyKey[] = ["constellation", "custom_btc", "custom_eth"];
const ASSET_PICK: StrategyKey[] = ["constellation", "ai_strat", "mean_reversion", "pocket_aces"];

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
  const locked = k === "pocket_aces";
  const field = (f: F) => <NumberField key={f.path} label={f.label} hint={f.hint} step={f.step} suffix={f.suffix}
    value={get(draft, f.path)} onChange={(v) => upd(f.path, v)} />;

  return (
    <div className="space-y-6">
      <div className="glass glass-gold flex flex-wrap items-start justify-between gap-4 p-5">
        <div className="max-w-2xl">
          <h3 className="font-brand text-2xl text-amber-hi">{strategy.name}</h3>
          <p className="mt-1.5 text-[13px] leading-relaxed text-tx">{strategy.description}</p>
          <p className="mt-2 font-num text-[11px] text-tx-dim">config v{strategy.version}</p>
        </div>
        <label className="flex items-center gap-3 rounded-full border border-line-mid bg-bg-0/50 px-4 py-2">
          <span className={cn("font-num text-xs font-bold tracking-wider", strategy.enabled ? "text-up" : "text-tx-low")}>{strategy.enabled ? "ENABLED" : "DISABLED"}</span>
          <Switch checked={strategy.enabled} disabled={toggle.isPending} onCheckedChange={(v) => toggle.mutate(v)} aria-label={`enable ${strategy.name}`} />
        </label>
      </div>

      <Scorecard strategy={k} />
      {k === "ai_strat" && <AiModelPanel />}

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

      {k !== "ai_strat" && !locked && (
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
