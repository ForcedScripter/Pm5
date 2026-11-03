import { useAuth } from "@/stores/auth";
import type { HealthOut, Readiness, SettingsOut, Strategy, WalletOut, LogEntry } from "@/types/api";

export class ApiError extends Error {
  status: number;
  detail: unknown;
  constructor(status: number, detail: unknown) {
    super(typeof detail === "string" ? detail : formatDetail(detail));
    this.status = status;
    this.detail = detail;
  }
}

function formatDetail(d: unknown): string {
  if (Array.isArray(d)) return d.map((e) => `${(e.loc ?? []).slice(1).join(".") || "input"}: ${e.msg}`).join("; ");
  if (d && typeof d === "object" && "message" in d) {
    const o = d as { message: string; failed?: string[] };
    return o.failed ? `${o.message}: ${o.failed.join(", ")}` : o.message;
  }
  return "request failed";
}

export async function api<T>(path: string, init: RequestInit & { json?: unknown } = {}): Promise<T> {
  const token = useAuth.getState().token;
  const headers: Record<string, string> = { ...(init.headers as Record<string, string>) };
  if (token) headers.Authorization = `Bearer ${token}`;
  let body = init.body;
  if (init.json !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(init.json);
  }
  const r = await fetch(path, { ...init, headers, body });
  if (r.status === 401 && token) useAuth.getState().logout();
  const data = r.headers.get("content-type")?.includes("json") ? await r.json() : await r.text();
  if (!r.ok) throw new ApiError(r.status, (data as { detail?: unknown })?.detail ?? data);
  return data as T;
}

export const Api = {
  health: () => api<HealthOut>("/api/health"),
  login: (username: string, password: string) =>
    api<{ token: string; username: string }>("/api/auth/login", { method: "POST", json: { username, password } }),
  wallet: () => api<WalletOut>("/api/wallet"),
  connectWallet: (b: { private_key: string; funder_address: string | null; signature_type: number }) =>
    api<WalletOut>("/api/wallet", { method: "POST", json: b }),
  disconnectWallet: () => api<{ ok: boolean }>("/api/wallet", { method: "DELETE" }),
  strategies: () => api<Strategy[]>("/api/strategies"),
  saveStrategy: (key: string, params: Record<string, unknown>) =>
    api<Strategy>(`/api/strategies/${key}`, { method: "PUT", json: { params } }),
  resetStrategy: (key: string) => api<Strategy>(`/api/strategies/${key}/reset`, { method: "POST" }),
  setStrategyEnabled: (key: string, on: boolean) =>
    api<Strategy>(`/api/strategies/${key}/${on ? "enable" : "disable"}`, { method: "POST" }),
  settings: () => api<SettingsOut>("/api/settings"),
  saveSettings: (b: Partial<Pick<SettingsOut, "paper_bankroll" | "risk" | "acceptance">>) =>
    api<SettingsOut>("/api/settings", { method: "POST", json: b }),
  readiness: () => api<Readiness>("/api/live-trading/readiness"),
  liveEnable: () => api<SettingsOut>("/api/live-trading/enable", { method: "POST" }),
  liveDisable: () => api<SettingsOut>("/api/live-trading/disable", { method: "POST" }),
  emergencyStop: () => api<SettingsOut>("/api/emergency-stop", { method: "POST" }),
  emergencyClear: () => api<SettingsOut>("/api/emergency-stop/clear", { method: "POST" }),
  logs: (category: string, before?: number) =>
    api<LogEntry[]>(`/api/logs?category=${category}${before ? `&before_id=${before}` : ""}`),
};
