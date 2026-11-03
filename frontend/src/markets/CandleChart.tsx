import { CandlestickSeries, ColorType, createChart, type IChartApi, type ISeriesApi, type UTCTimestamp } from "lightweight-charts";
import { useEffect, useRef } from "react";
import type { Candle } from "@/stores/market";

export function CandleChart({ candles, windowOpen }: { candles: Candle[]; windowOpen: number | null }) {
  const el = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  const series = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const lastLen = useRef(0);

  useEffect(() => {
    if (!el.current) return;
    const c = createChart(el.current, {
      autoSize: true,
      layout: { background: { type: ColorType.Solid, color: "transparent" }, textColor: "#a8987a", fontFamily: "Space Grotesk" },
      grid: { vertLines: { color: "rgba(198,178,140,0.06)" }, horzLines: { color: "rgba(198,178,140,0.06)" } },
      rightPriceScale: { borderColor: "rgba(198,178,140,0.2)" },
      timeScale: { borderColor: "rgba(198,178,140,0.2)", timeVisible: true, secondsVisible: false },
      crosshair: { vertLine: { color: "#f79240", labelBackgroundColor: "#4a4132" }, horzLine: { color: "#f79240", labelBackgroundColor: "#4a4132" } },
    });
    series.current = c.addSeries(CandlestickSeries, {
      upColor: "#35d49a", downColor: "#ff5a76", borderVisible: false, wickUpColor: "#35d49a", wickDownColor: "#ff5a76",
    });
    chart.current = c;
    return () => { c.remove(); chart.current = null; series.current = null; lastLen.current = 0; };
  }, []);

  useEffect(() => {
    const s = series.current;
    if (!s || !candles.length) return;
    const data = candles.map((x) => ({ ...x, time: x.time as UTCTimestamp }));
    if (Math.abs(candles.length - lastLen.current) > 1 || lastLen.current === 0) {
      s.setData(data);
      chart.current?.timeScale().fitContent();
    } else {
      s.update(data.at(-1)!);
    }
    lastLen.current = candles.length;
  }, [candles]);

  useEffect(() => {
    const s = series.current;
    if (!s || windowOpen == null) return;
    const line = s.createPriceLine({ price: windowOpen, color: "#f2c45a", lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: "round open" });
    return () => s.removePriceLine(line);
  }, [windowOpen]);

  return <div ref={el} className="h-full w-full" />;
}
