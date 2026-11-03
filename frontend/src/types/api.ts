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

export interface SignalRow {
  id: string; ts: string; strategy: StrategyKey; asset: string; side: "UP" | "DOWN";
  status: "accepted" | "rejected" | "executed" | "candidate"; code: string | null; reason: string;
  price: number; confidence: number | null; secs_in: number; round_start: number | null;
  outcome: "UP" | "DOWN" | null; won: boolean | null; pnl_per_dollar: number | null; mode: "paper" | "live";
}

export interface StrategyStats {
  strategy: StrategyKey; name: string; accepted: number; resolved: number; wins: number;
  hit_rate: number | null; avg_price: number | null; avg_pnl_per_dollar: number | null;
  total_pnl_per_dollar: number; breakeven_hit_rate: number | null;
  skips: { code: string; n: number; resolved: number; would_have_won: number | null }[];
}

export interface EngineStatus {
  bot_running: boolean; emergency_stopped: boolean; mode: "paper" | "live"; enabled: StrategyKey[];
  engine_online: boolean; active_model: string | null;
  engine: { last_tick: number | null; users: number; errors: number; last_error: string | null; evaluations: number;
    last_training?: { at: number; ok: boolean; activated: boolean | null; reason: string | null; version: string | null } } | null;
}

export interface Scores { n: number; base_rate: number | null; logloss: number; brier: number; accuracy: number; auc: number | null }
export interface SimResult { trades: number; hit_rate: number | null; avg_pnl_per_dollar: number | null; total_pnl_per_dollar: number }
export interface ModelSummary {
  version: string; kind: string; trained_at: string | null; active: boolean; n: { train: number; val: number; test: number };
  data_from: string; data_to: string;
  metrics: { train: Scores; val: Scores; test: Scores; market_test: Scores; beats_market: boolean; rounds: number;
    sim_test: SimResult; sim_test_market?: SimResult; coefficients: Record<string, number>;
    calibration_test: { bin: string; n: number; mean_p: number; hit: number }[] };
}
export interface AiModels { models: ModelSummary[]; settled_rounds: number; rounds: number; predictions_logged: number }
export type TrainResult = { ok: boolean; reason?: string; activated?: boolean; version?: string; rounds?: number };

export interface Portfolio {
  mode: "paper" | "live"; cash: number; positions_value: number; equity: number; invested: number; realized_pnl: number;
  unrealized_pnl: number; today_realized: number; trades: number; wins: number; win_rate: number | null; open_positions: number;
  peak_equity: number; drawdown_pct: number; start_equity: number; cash_note?: string;
}
export interface PositionRow {
  id: string; mode: string; strategy: string; asset: string; side: "UP" | "DOWN"; status: string; shares: number; cost_basis: number;
  avg_price: number | null; fees: number; first_entry_price: number | null; dca_steps: number; bid: number | null; value: number | null;
  unrealized_pnl: number | null; realized_pnl: number; opened_at: string | null; closed_at: string | null; window_end: string | null;
  market: string | null; exchange_shares: number | null; reconciled_at: string | null;
}
export interface OrderRow {
  id: string; ts: string | null; asset: string; purpose: string; side: "BUY" | "SELL"; type: string; limit: number; requested: number;
  filled_shares: number; filled_cost: number; fees: number; status: string; error: string | null; exchange_order_id: string | null;
  fills: { price: number; shares: number; fee: number; status: string; tx_hash: string | null }[]; mode: string;
}
export interface TradeRow {
  id: string; mode: string; strategy: string; asset: string; side: "UP" | "DOWN"; result: "won" | "lost" | "stopped" | "sold";
  entry_avg: number; exit_price: number; shares: number; cost: number; proceeds: number; fees: number; pnl: number;
  opened_at: string; closed_at: string; duration_s: number;
}
