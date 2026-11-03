import { useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";
import { useAuth } from "@/stores/auth";

/** /ws/portfolio: refresh positions, orders, trades and portfolio whenever the order manager acts. */
export function usePortfolioStream() {
  const qc = useQueryClient();
  useEffect(() => {
    let ws: WebSocket | null = null, stop = false, retry = 0;
    const connect = () => {
      ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/portfolio`);
      ws.onopen = () => ws?.send(JSON.stringify({ type: "auth", token: useAuth.getState().token }));
      ws.onmessage = (ev) => {
        const m = JSON.parse(ev.data);
        if (m.type === "ready") retry = 0;
        if (["order", "position", "portfolio", "emergency"].includes(m.type)) {
          for (const k of ["portfolio", "positions", "orders", "trades", "logs", "readiness"]) qc.invalidateQueries({ queryKey: [k] });
        }
      };
      ws.onclose = () => { if (!stop) setTimeout(connect, Math.min(30000, 1000 * 2 ** retry++)); };
    };
    connect();
    return () => { stop = true; ws?.close(); };
  }, [qc]);
}
