/** Money stays decimal text. BigInt arithmetic is used only for UI estimates. */
function parts(value: string): { units: bigint; scale: number } {
  if (!/^-?\d+(?:\.\d+)?$/.test(value)) throw new Error('Invalid decimal amount');
  const [whole, fraction = ''] = value.split('.');
  return { units: BigInt(whole + fraction), scale: fraction.length };
}
function text(units: bigint, scale: number): string {
  const negative = units < 0n;
  const digits = (negative ? -units : units).toString().padStart(scale + 1, '0');
  return `${negative ? '-' : ''}${scale ? `${digits.slice(0, -scale)}.${digits.slice(-scale)}` : digits}`;
}
export function addDecimals(values: string[]): string {
  const parsed = values.map(parts);
  const scale = Math.max(0, ...parsed.map((value) => value.scale));
  return text(parsed.reduce((sum, value) => sum + value.units * 10n ** BigInt(scale - value.scale), 0n), scale);
}
export function compareDecimals(left: string, right: string): number {
  const a = parts(left), b = parts(right);
  const scale = Math.max(a.scale, b.scale);
  const difference = a.units * 10n ** BigInt(scale - a.scale) - b.units * 10n ** BigInt(scale - b.scale);
  return difference < 0n ? -1 : difference > 0n ? 1 : 0;
}
/** Approximate geometry only; no conversion of arbitrary-size money to Number. */
export function decimalShare(value: string, total: string): number | null {
  const a = parts(value), b = parts(total);
  if (a.units < 0n || b.units <= 0n) return null;
  const scale = Math.max(a.scale, b.scale);
  const numerator = a.units * 10n ** BigInt(scale - a.scale);
  const denominator = b.units * 10n ** BigInt(scale - b.scale);
  if (numerator > denominator) return null;
  return Number(numerator * 1_000_000_000_000n / denominator) / 1_000_000_000_000;
}
export function scaleDecimal(value: string, numerator: number, denominator: number): string {
  if (!Number.isSafeInteger(numerator) || !Number.isSafeInteger(denominator) || denominator <= 0) throw new Error('Invalid estimate ratio');
  const parsed = parts(value);
  const scale = Math.max(12, parsed.scale);
  return text(parsed.units * 10n ** BigInt(scale - parsed.scale) * BigInt(numerator) / BigInt(denominator), scale);
}
export function formatDecimalMoney(amount: string, currency: string): string {
  const parsed = parts(amount);
  const formatter = new Intl.NumberFormat('en', { style: 'currency', currency });
  const digits = formatter.resolvedOptions().maximumFractionDigits ?? 2;
  const negative = parsed.units < 0n;
  let magnitude = negative ? -parsed.units : parsed.units;
  if (parsed.scale > digits) {
    const divisor = 10n ** BigInt(parsed.scale - digits);
    magnitude = (magnitude + divisor / 2n) / divisor;
  } else magnitude *= 10n ** BigInt(digits - parsed.scale);
  const divisor = 10n ** BigInt(digits);
  const whole = magnitude / divisor;
  const fraction = (magnitude % divisor).toString().padStart(digits, '0');
  // Intl formats arbitrary-size integers without converting them to Number.
  return `${negative ? '-' : ''}${formatter.formatToParts(whole).map((part) => part.type === 'fraction' ? fraction : part.value).join('')}`;
}

/** Compact currency presentation without rendering a nonzero amount as zero. */
export function formatCompactDecimalMoney(amount: string, currency: string): string {
  const formatted = formatDecimalMoney(amount, currency);
  const parsed = parts(amount);
  const sign = parsed.units < 0n ? -1 : parsed.units > 0n ? 1 : 0;
  const digits = new Intl.NumberFormat('en', { style: 'currency', currency })
    .resolvedOptions().maximumFractionDigits ?? 2;
  if (sign !== 0 && formatted.replace(/^-/, '') === formatDecimalMoney('0', currency)) {
    const minimum = digits ? `0.${'0'.repeat(digits - 1)}1` : '1';
    const threshold = formatDecimalMoney(minimum, currency);
    return sign > 0 ? `<${threshold}` : `>-${threshold}`;
  }
  // Costs are estimates already labelled as such; rounding to cents needs no extra mark.
  return formatted;
}

/** Hover text for an estimate: the rounded value and its currency, never raw precision. */
export function moneyTitle(amount: string | null | undefined, currency: string | null | undefined): string | undefined {
  if (!amount || !currency) return undefined;
  try { return `${formatCompactDecimalMoney(amount, currency)} ${currency} · estimated`; } catch { return undefined; }
}
