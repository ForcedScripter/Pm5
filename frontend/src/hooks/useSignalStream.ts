import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { useAuth } from "@/stores/auth";

export interface LiveSignal { strategy: string; asset: string; side: string; status: string; code: string; reason: string;
  price: number | null; confidence: number | null; secs_in: number; ts: number }

/** Subscribes to /ws/signals; refreshes signal/log queries whenever the engine emits one. */
export function useSignalStream(onSignal?: (s: LiveSignal) => void) {
  const qc = useQueryClient();
  const cb = useRef(onSignal);
  cb.current = onSignal;
  const [connected, setConnected] = useState(false);
  useEffect(() => {
    let ws: WebSocket | null = null, stop = false, retry = 0;
    const connect = () => {
      const proto = location.protocol === "https:" ? "wss" : "ws";
      ws = new WebSocket(`${proto}://${location.host}/ws/signals`);
      ws.onopen = () => ws?.send(JSON.stringify({ type: "auth", token: useAuth.getState().token }));
      ws.onmessage = (ev) => {
        const m = JSON.parse(ev.data);
        if (m.type === "ready") { setConnected(true); retry = 0; }
        if (m.type !== "signal") return;
        cb.current?.(m);
        qc.invalidateQueries({ queryKey: ["signals"] });
        qc.invalidateQueries({ queryKey: ["logs"] });
        qc.invalidateQueries({ queryKey: ["signal-stats"] });
      };
      ws.onclose = () => { setConnected(false); if (!stop) setTimeout(connect, Math.min(30000, 1000 * 2 ** retry++)); };
    };
    connect();
    return () => { stop = true; ws?.close(); };
  }, [qc]);
  return connected;
}
