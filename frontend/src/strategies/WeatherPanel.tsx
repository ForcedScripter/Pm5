import { useQuery } from "@tanstack/react-query";
import { CloudSun } from "lucide-react";
import { Empty, Panel, PanelHeader } from "@/components/ui/panel";
import { cn } from "@/lib/utils";
import { Api } from "@/services/api";

const t = (v: number | null | undefined, d = 1) => (v == null ? "—" : `${v.toFixed(d)}°C`);

/** Weather data being logged (no orders): the airport reports that settle each market, the ensemble
 *  forecast, and the price ladder. Refreshes once a minute; the data itself changes every 10. */
export function WeatherPanel() {
  const q = useQuery({ queryKey: ["weather-summary"], queryFn: () => Api.weatherSummary(), refetchInterval: 60_000 });
  const d = q.data;
  const open = (d?.events ?? []).filter((e) => !e.winner).slice(0, 24);
  return (
    <Panel>
      <PanelHeader icon={<CloudSun />} title="Weather data (collecting, not trading)" />
      {d?.note && <p className="border-b border-line px-4 py-2 text-[11.5px] leading-relaxed text-tx-low">{d.note}</p>}
      {!d || d.cities.length === 0 ? <Empty>{q.isLoading ? "Loading…" : "Nothing logged yet. The collector writes its first rows about 10 minutes after the server starts."}</Empty> : (
        <>
          <div className="overflow-x-auto border-b border-line">
            <table className="w-full min-w-[720px] text-[12.5px]">
              <thead><tr className="border-b border-line text-left font-num text-[10.5px] tracking-wider text-tx-low uppercase">
                {["City", "Airport", "Days logged", "Resolved", "Our maximum vs Polymarket", "Forecast bias", "Forecast avg error"].map((h) => <th key={h} className="px-3 py-2">{h}</th>)}
              </tr></thead>
              <tbody>{d.cities.map((c) => (
                <tr key={c.city} className="border-b border-line/60">
                  <td className="px-3 py-1.5 font-semibold text-tx-hi">{c.city}</td>
                  <td className="num px-3 py-1.5">{c.station}</td>
                  <td className="num px-3 py-1.5">{c.days}</td>
                  <td className="num px-3 py-1.5">{c.resolved}</td>
                  <td className={cn("num px-3 py-1.5", c.checked > 0 && c.agree < c.checked ? "text-down" : "")}>{c.checked === 0 ? "—" : `${c.agree} of ${c.checked} agree`}</td>
                  <td className="num px-3 py-1.5" title="Last logged ensemble mean minus the actual maximum">{t(c.forecast_bias_c, 2)}</td>
                  <td className="num px-3 py-1.5">{t(c.forecast_mae_c, 2)}</td>
                </tr>))}</tbody>
            </table>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full min-w-[820px] text-[12.5px]">
              <thead><tr className="border-b border-line text-left font-num text-[10.5px] tracking-wider text-tx-low uppercase">
                {["Date", "City", "Max so far", "Forecast (low / mean / high)", "Market favourite", "Rows"].map((h) => <th key={h} className="px-3 py-2">{h}</th>)}
              </tr></thead>
              <tbody>{open.map((e) => (
                <tr key={e.slug} className="border-b border-line/60">
                  <td className="num px-3 py-1.5 text-tx-dim">{e.date}</td>
                  <td className="px-3 py-1.5 font-semibold text-tx-hi">{e.city}</td>
                  <td className="num px-3 py-1.5">{e.running_max_c == null ? "—" : `${e.running_max_c.toFixed(1)}°C → ${e.running_whole}°${e.unit}`}</td>
                  <td className="num px-3 py-1.5">{e.forecast_mean_c == null ? "—" : `${t(e.forecast_min_c)} / ${t(e.forecast_mean_c)} / ${t(e.forecast_max_c)}`}</td>
                  <td className="num px-3 py-1.5">{e.market_favourite ? `${e.market_favourite} at ${((e.market_favourite_price ?? 0) * 100).toFixed(0)}¢` : "—"}</td>
                  <td className="num px-3 py-1.5 text-tx-low">{e.samples}</td>
                </tr>))}</tbody>
            </table>
          </div>
        </>
      )}
    </Panel>
  );
}
