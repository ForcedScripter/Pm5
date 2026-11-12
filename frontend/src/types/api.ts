export interface HealthOut {
  status: "ok" | "degraded";
  db: boolean;
  redis: boolean;
  config_problems: string[];
  server_time: number;
  feeds?: Record<string, { name: string; status: string; last_msg_age_s: number | null; reconnects: number; last_error: string | null }>;
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

export type StrategyKey = "binance_lead" | "twap_lock" | "constellation" | "odds_scalper" | "pocket_aces" | "spork_maker" | "crypto_strikes" | "pair_arb" | "custom_btc";

export interface Strategy {
  key: StrategyKey;
  name: string;
  description: string;
  enabled: boolean;
  version: number;
  params: Record<string, unknown>;
  defaults: Record<string, unknown>;
  edge_decay?: { paused: boolean; mean_pnl: number | null; n: number; window: number } | null;
}

export type ScoreVerdict = "passing" | "losing" | "inconclusive" | "not enough data";

export interface ScoreBand {
  band: string; n: number; win_rate: number | null; pnl: number; pnl_per_trade: number | null;
  ci_low: number | null; ci_high: number | null; verdict: ScoreVerdict;
}

export interface ScoreRow extends Omit<ScoreBand, "band"> {
  strategy: string; name: string; unit: "trade" | "round"; wins: number; need: number;
  breakeven_win_rate: number | null; avg_entry: number | null; max_day_share: number | null; bands: ScoreBand[];
  strict?: Omit<ScoreBand, "band" | "win_rate">;
}

export interface Scoreboard {
  mode: string; strategies: ScoreRow[];
  calibration: { bucket: string; n: number; mean_fair: number; win_rate: number }[];
  rule: { min_trades: number; min_rounds: number; min_for_verdict: number; max_day_share: number };
}

export interface ArbPairRow {
  id: string; asset: string; status: string; shares: number; up_price: number; down_price: number; cost: number; fees: number;
  locked_pnl: number; pnl: number | null; secs_in: number | null; opened_at: string; settled_at: string | null;
}
export interface ArbStats {
  mode: string; note: string; pairs: number; open: number; pnl: number; locked_open: number; staked: number;
  engine: { scans: number; opportunities: number; missed: number; pairs: number; best_cost: number | null; last_block: string | null } | null;
}
export interface WeatherSummary {
  status: Record<string, unknown> | null; metars: number; note: string;
  cities: { city: string; station: string; days: number; resolved: number; checked: number; agree: number;
            forecast_bias_c: number | null; forecast_mae_c: number | null }[];
  events: { slug: string; city: string; station: string; date: string; unit: string; samples: number; running_max_c: number | null;
            running_whole: number | null; forecast_mean_c: number | null; forecast_min_c: number | null; forecast_max_c: number | null;
            market_favourite: string | null; market_favourite_price: number | null; winner: string | null; own_label: string | null;
            own_max_c: number | null }[];
}
export interface StrikeMarketRow {
  id: string; slug: string; event: string; asset: string; kind: "above" | "reach" | "dip"; strike: number; expiry: number;
  model: number | null; vol: number | null; bid: number | null; ask: number | null; book_age_s: number | null;
  resting: Record<string, { price: number; shares: number }>;
}
export interface StrikeEngineStatus {
  markets: number; books: number; quotes: number; fills: number; errors: number; last_error: string | null;
  last_poll: number | null; last_discover: number | null;
}
export interface StrikePositionRow {
  id: string; slug: string; asset: string; kind: string; strike: number; expiry: string; side: "YES" | "NO"; status: string;
  shares: number; entry_price: number; cost: number; model_prob: number; mark: number | null; exit_price: number | null;
  fees: number; pnl: number | null; opened_at: string; closed_at: string | null; winner: string | null;
}
export interface StrikeStats {
  mode: string; disclaimer: string;
  calibration: { kind: string; horizon: string; n: number; model_brier: number | null; market_brier: number | null }[];
  settlement: { checked: number; agree: number; disagree: number };
  positions: { open: number; open_cost: number; closed: number; wins: number; pnl: number };
}
export interface MakerRound {
  id: string; asset: string; status: string; mode: string; sets: number; sold_up: number; sold_down: number;
  proceeds: number; merged_sets: number | null; residual_side: "UP" | "DOWN" | null; residual_shares: number | null;
  redeem_value: number | null; pnl: number | null; rebate_est: number; edge: number; fills: number;
  one_sided: boolean | null; winner: "UP" | "DOWN" | null; markout_5s: number | null; markout_30s: number | null;
  pnl_strict: number | null; reward_s: number | null; reward_sized: boolean;
  window_end: string; settled_at: string | null;
}

export interface MakerStat {
  asset: string; rounds: number; pnl: number; pnl_per_round: number | null; win_rounds: number;
  one_sided_share: number | null; paired_shares: number; fills: number; markout_5s: number | null;
  markout_30s: number | null; rebate_est: number; pnl_strict: number | null; strict_rounds: number; reward_s: number;
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

export interface Metrics {
  trades: number; wins?: number; losses?: number; win_rate?: number; win_rate_ci95?: [number, number] | null;
  breakeven_win_rate?: number | null; net_pnl?: number; gross_profit?: number; gross_loss?: number; profit_factor?: number | null;
  avg_win?: number | null; avg_loss?: number | null; expectancy?: number; roi_on_deployed?: number | null; total_return?: number | null;
  max_drawdown?: number; max_drawdown_pct?: number; sharpe_per_trade?: number | null; sharpe_daily_annualized?: number | null;
  trades_per_day?: number; span_days?: number; fees?: number; deployed?: number; equity_curve?: { t: number; equity: number }[]; note?: string;
}
export interface BtTrade { pnl: number; cost: number; fees: number; opened_at: number; closed_at: number; strategy: string; asset: string;
  side: string; result: string; entry_price: number; avg_price: number | null; shares: number; fidelity: string; dca_stages: number }
export interface BacktestResults {
  overall: Metrics; by_strategy: Record<string, Metrics>; by_asset: Record<string, Metrics>; by_fidelity: Record<string, Metrics>;
  split: { split_at: number; in_sample: Metrics; out_of_sample: Metrics } | null; counts: Record<string, number>;
  skips: Record<string, number>; risk_blocks: Record<string, number>; rounds: { total: number; recorded?: number; backfill?: number; windows: number };
  half_spread_model: number; trades: BtTrade[]; warnings: string[];
}
export interface BacktestSummary { id: string; name: string; status: "queued" | "running" | "done" | "failed"; created_at: string | null;
  finished_at: string | null; error: string | null; progress: number; period: [string, string]; strategies: string[];
  trades: number | null; net_pnl: number | null; win_rate: number | null; profit_factor: number | null }
export interface BacktestDetail extends BacktestSummary { params: Record<string, unknown>; results: BacktestResults | null }
export interface Coverage { settled_rounds: number; recorded_rounds: number; backfilled_rounds: number; first_round: string | null;
  last_round: string | null; spot_bars: { from: string | null; to: string | null; count: number } }
export interface BackfillProgress { running?: boolean; stage?: string; done?: number; total?: number; counts?: Record<string, number>;
  error?: string | null; seconds?: number; bars_added?: number }
