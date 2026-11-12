import { describe, expect, it } from 'vitest';
import { addDecimals, formatCompactDecimalMoney, formatDecimalMoney, scaleDecimal } from './decimal';
describe('exact decimal UI monetary values', () => {
  it('preserves decimal addition and values beyond IEEE-754 integer precision', () => {
    expect(addDecimals(['0.1', '0.2'])).toBe('0.3');
    expect(addDecimals(['9007199254740993.000001', '0.000009'])).toBe('9007199254740993.000010');
    expect(formatDecimalMoney('9007199254740993.126', 'USD')).toBe('$9,007,199,254,740,993.13');
    expect(formatDecimalMoney('0.004', 'USD')).toBe('$0.00');
    expect(formatDecimalMoney('0.005', 'USD')).toBe('$0.01');
  });
  it('handles currency precision, negatives and deterministic forecast ratios', () => {
    expect(formatDecimalMoney('12.7', 'JPY')).toBe('¥13');
    expect(formatDecimalMoney('-0.025', 'USD')).toBe('-$0.03');
    expect(scaleDecimal('0.3', 7, 3)).toBe('0.700000000000');
    expect(() => addDecimals(['NaN'])).toThrow('Invalid decimal');
  });
  it('shows compact amounts without turning small nonzero values into zero', () => {
    expect(formatCompactDecimalMoney('0.0880000000000000', 'USD')).toBe('$0.09');
    expect(formatCompactDecimalMoney('0.000000000001', 'USD')).toBe('<$0.01');
    expect(formatCompactDecimalMoney('-0.000000000001', 'USD')).toBe('>-$0.01');
    expect(formatCompactDecimalMoney('0.000000', 'USD')).toBe('$0.00');
    expect(formatCompactDecimalMoney('1.200000', 'USD')).toBe('$1.20');
    expect(formatCompactDecimalMoney('0.00001', 'JPY')).toBe('<¥1');
  });
});
