export interface HealthOut {
  status: "ok" | "degraded";
  db: boolean;
  redis: boolean;
  config_problems: string[];
  server_time: number;
}

export interface WalletOut {
  connected: boolean;
  signer_address: string | null;
  funder_address: string | null;
  signature_type: number | null;
  source: "app" | "env" | null;
  last_verified_at: string | null;
  api_creds_ok: boolean;
}

export type StrategyKey = "constellation" | "ai_strat" | "mean_reversion" | "custom_btc" | "custom_eth" | "pocket_aces";

export interface Strategy {
  key: StrategyKey;
  name: string;
  description: string;
  enabled: boolean;
  version: number;
  params: Record<string, unknown>;
  defaults: Record<string, unknown>;
}

export interface RiskSettings {
  max_position_usd: number;
  max_trade_usd: number;
  max_daily_loss_usd: number;
  max_daily_exposure_usd: number;
  max_open_positions: number;
  max_trades_per_market: number;
  max_drawdown_pct: number;
  min_liquidity_usd: number;
  max_spread: number;
  max_slippage: number;
  min_seconds_before_resolution: number;
  loss_cooldown_s: number;
  consecutive_losses_for_cooldown: number;
}

export interface Acceptance {
  min_paper_days: number;
  min_paper_trades: number;
  min_win_rate: number;
  min_profit_factor: number;
}

export interface SettingsOut {
  live_trading_enabled: boolean;
  live_allowed_by_server: boolean;
  bot_running: boolean;
  emergency_stopped: boolean;
  paper_bankroll: number;
  risk: RiskSettings;
  acceptance: Acceptance;
}

export interface Readiness {
  ready: boolean;
  checks: Record<string, boolean>;
  paper: { trades: number; wins: number; pnl: number; days: number; win_rate: number; profit_factor: number };
  criteria: Acceptance;
}

export interface LogEntry {
  id: number;
  ts: string;
  level: "debug" | "info" | "warn" | "error";
  category: string;
  message: string;
  data: Record<string, unknown>;
}
