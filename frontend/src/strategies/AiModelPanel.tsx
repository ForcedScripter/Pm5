import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { BrainCircuit } from "lucide-react";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Segmented } from "@/components/ui/form";
import { Badge } from "@/components/ui/panel";
import { cn } from "@/lib/utils";
import { Api } from "@/services/api";

const f3 = (v: number | null | undefined) => (v == null ? "—" : v.toFixed(3));
const pct = (v: number | null | undefined) => (v == null ? "—" : `${(v * 100).toFixed(1)}%`);

export function AiModelPanel() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["ai-models"], queryFn: Api.aiModels, refetchInterval: 60_000 });
  const [kind, setKind] = useState<"logreg" | "gbm">("logreg");
  const train = useMutation({ mutationFn: () => Api.aiTrain(kind, 3), onSuccess: () => qc.invalidateQueries({ queryKey: ["ai-models"] }) });
  const latest = q.data?.models[0];
  const active = q.data?.models.find((m) => m.active);
  const m = latest?.metrics;
  return (
    <div className="glass space-y-4 p-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-2"><BrainCircuit className="h-5 w-5 text-azure" /><span className="panel-title">Model</span>
          {active ? <Badge className="border-up/50 text-up">active · {active.version}</Badge> : <Badge className="border-amber/50 text-amber">no active model · AI Strat stays idle</Badge>}</div>
        <span className="text-[11.5px] text-tx-low">{q.data ? `${q.data.settled_rounds} settled markets collected · ${q.data.predictions_logged} live predictions logged` : ""}</span>
      </div>
      <p className="text-[12.5px] leading-relaxed text-tx">
        Trained on this server's own history: 1-second prices, 5-second order-book samples and settled winners, sampled 45–210s into each round using only data available at that moment.
        Rounds are split by time: 60% train, 20% calibration, and the latest 20% held out as test. A model is <b>activated only if it predicts the held-out rounds better than the market's own prices</b>.
        Otherwise it has no tradable edge and AI Strat places nothing.
      </p>
      {latest && m && (
        <div className="space-y-3">
          <div className="grid grid-cols-2 gap-2.5 md:grid-cols-4">
            {[
              ["Test log-loss", f3(m.test.logloss), `market ${f3(m.market_test.logloss)}`, m.beats_market ? "text-up" : "text-down"],
              ["Test AUC", f3(m.test.auc), `market ${f3(m.market_test.auc)}`, ""],
              ["Test accuracy", pct(m.test.accuracy), `${m.test.n} samples · ${m.rounds} rounds`, ""],
              ["Simulated test trades", m.sim_test.trades, m.sim_test.trades ? `hit ${pct(m.sim_test.hit_rate)} · ${m.sim_test.avg_pnl_per_dollar != null ? (m.sim_test.avg_pnl_per_dollar >= 0 ? "+" : "") + m.sim_test.avg_pnl_per_dollar.toFixed(3) + "/$" : ""}` : "no qualifying entries", ""],
            ].map(([l, v, sub, tone]) => (
              <div key={l as string} className="rounded-2xl border border-line bg-bg-0/40 px-4 py-3">
                <div className="panel-title !text-[10px]">{l}</div><div className={cn("num mt-1 text-lg font-semibold text-tx-hi", tone as string)}>{v}</div>
                <div className="text-[10.5px] text-tx-low">{sub}</div>
              </div>
            ))}
          </div>
          <p className={cn("text-[12px]", m.beats_market ? "text-up" : "text-warn")}>
            {latest.version}: {m.beats_market ? "beat the market on held-out rounds, so it was activated." : "did not beat the market's prices on held-out rounds, so it was not activated."}
          </p>
          {Object.keys(m.coefficients).length > 0 && (
            <div className="flex flex-wrap gap-1.5 text-[11px]"><span className="font-num tracking-wider text-tx-low uppercase">Strongest inputs</span>
              {Object.entries(m.coefficients).slice(0, 6).map(([k, v]) => <span key={k} className="rounded-full border border-line-mid px-2 py-0.5 text-tx">{k} <span className={v >= 0 ? "text-up" : "text-down"}>{v >= 0 ? "+" : "−"}{Math.abs(v).toFixed(2)}</span></span>)}
            </div>
          )}
        </div>
      )}
      <div className="flex flex-wrap items-center gap-3">
        <div className="w-64"><Segmented value={kind} onChange={setKind} options={[{ value: "logreg", label: "Logistic" }, { value: "gbm", label: "Gradient boost" }]} /></div>
        <Button variant="amber" loading={train.isPending} onClick={() => train.mutate()}>Train now</Button>
        {train.data && <span className={cn("text-[12px]", train.data.ok ? (train.data.activated ? "text-up" : "text-warn") : "text-tx-low")}>
          {train.data.ok ? (train.data.activated ? `Trained ${train.data.version}: activated.` : `Trained ${train.data.version}: no edge over market, not activated.`) : train.data.reason}</span>}
        {train.error && <span className="text-[12px] text-down">{train.error.message}</span>}
      </div>
      <p className="text-[11px] text-tx-dim">Retrains automatically every 6 hours on the latest 3 days.</p>
    </div>
  );
}
