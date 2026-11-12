/** Linear trend projection over complete usage buckets (ordinary least squares). */

export interface ForecastPoint { at: number; value: number; low: number; high: number }
export interface Forecast {
  /** Projected bucket values after the last complete bucket. */
  points: ForecastPoint[];
  /** Sum over the projected buckets with its 80 % range. */
  total: { value: number; low: number; high: number };
  /** Average projected value per bucket. */
  rate: number;
  /** Least-squares slope per bucket (direction of the trend). */
  slope: number;
  /** Buckets the fit used. */
  samples: number;
}

export const MIN_FORECAST_SAMPLES = 4;
/** z for a two-sided 80 % normal interval. */
const Z80 = 1.2816;

/**
 * Fit `value = a + b·index` over known complete buckets and project `horizon`
 * buckets after the last one. The range widens with distance from the fitted
 * mean (standard prediction interval). Values never go below zero. Returns
 * null when fewer than MIN_FORECAST_SAMPLES buckets are known.
 */
export function linearForecast(history: { at: number; value: number | null }[], step: number, horizon: number): Forecast | null {
  const known = history.map((point, index) => ({ index, at: point.at, value: point.value }))
    .filter((point): point is { index: number; at: number; value: number } => point.value !== null && Number.isFinite(point.value));
  const n = known.length;
  if (n < MIN_FORECAST_SAMPLES || horizon < 1) return null;
  const meanX = known.reduce((sum, p) => sum + p.index, 0) / n;
  const meanY = known.reduce((sum, p) => sum + p.value, 0) / n;
  const sxx = known.reduce((sum, p) => sum + (p.index - meanX) ** 2, 0);
  const slope = sxx > 0 ? known.reduce((sum, p) => sum + (p.index - meanX) * (p.value - meanY), 0) / sxx : 0;
  const intercept = meanY - slope * meanX;
  const residual = Math.sqrt(known.reduce((sum, p) => sum + (p.value - (intercept + slope * p.index)) ** 2, 0) / Math.max(1, n - 2));
  const last = history.length - 1;
  const lastAt = history[last].at;
  const points: ForecastPoint[] = [];
  let total = 0, variance = 0;
  for (let k = 1; k <= horizon; k++) {
    const x = last + k;
    const value = Math.max(0, intercept + slope * x);
    const spread = residual * Math.sqrt(1 + 1 / n + (sxx > 0 ? (x - meanX) ** 2 / sxx : 0));
    points.push({ at: lastAt + k * step, value, low: Math.max(0, value - Z80 * spread), high: value + Z80 * spread });
    total += value;
    variance += spread ** 2;
  }
  const totalSpread = Z80 * Math.sqrt(variance);
  return { points, total: { value: total, low: Math.max(0, total - totalSpread), high: total + totalSpread },
    rate: total / horizon, slope, samples: n };
}
