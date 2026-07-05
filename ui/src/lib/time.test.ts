import { describe, expect, it } from 'vitest';

import { formatCompactTime, formatRelativeTime } from './time';

describe('time formatting', () => {
  it('does not present missing timestamps as just now', () => {
    expect(formatRelativeTime(null)).toBe('');
    expect(formatCompactTime(null)).toBe('');
  });
});
