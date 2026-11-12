/** Mirrors backend/app/strategies/catalog.py MODE_PRESETS and DCA presets (reference values). */
export const MODE_PRESETS = {
  selective: { up_threshold: 0.08, down_threshold: 0.14, entry_low: 0.42, entry_high: 0.62, max_concurrent: 2, min_consensus: 4 },
  balanced: { up_threshold: 0.055, down_threshold: 0.1, entry_low: 0.38, entry_high: 0.68, max_concurrent: 3, min_consensus: 4 },
  aggressive: { up_threshold: 0.04, down_threshold: 0.07, entry_low: 0.35, entry_high: 0.75, max_concurrent: 4, min_consensus: 4 },
} as const;

export const DCA_PRESETS = {
  off: { stages: [5], max_total: 5 },
  capped: { stages: [5, 2], max_total: 7 },
  standard: { stages: [5, 3, 2.5], max_total: 10.5 },
  confirm: { stages: [5, 3, 2.5], max_total: 10.5 },
} as const;

export const MODE_OPTIONS = [
  { value: "selective", label: "Selective", sub: "rare · high quality" },
  { value: "balanced", label: "Balanced", sub: "moderate" },
  { value: "aggressive", label: "Aggressive", sub: "most windows" },
] as const;

export const DCA_OPTIONS = [
  { value: "standard", label: "Standard", sub: "+$3 then +$2.50 · ceiling $10.50" },
  { value: "confirm", label: "On confirm", sub: "adds only if spot still agrees" },
  { value: "capped", label: "Capped", sub: "one $2 top-up · ceiling $7" },
  { value: "off", label: "Off", sub: "single position" },
] as const;

/** Real BTC hourly volatility (0–100) by UTC hour, from the reference page's 60-day 1-min study. */
export const VOL_BY_HOUR = [26, 32, 21, 10, 2, 7, 4, 2, 7, 1, 0, 5, 36, 78, 100, 73, 53, 39, 29, 28, 11, 12, 28, 10];
