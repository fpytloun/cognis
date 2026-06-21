import { describe, expect, it } from 'vitest';

import { matchesScheduleVisibility, type ScheduleVisibilityFilter } from './schedules';

describe('matchesScheduleVisibility', () => {
  const active = { is_expired: false };
  const expired = { is_expired: true };

  it.each([
    ['active', true, false],
    ['expired', false, true],
    ['all', true, true]
  ] satisfies [ScheduleVisibilityFilter, boolean, boolean][])(
    'filters %s schedules',
    (filter, activeMatch, expiredMatch) => {
      expect(matchesScheduleVisibility(active, filter)).toBe(activeMatch);
      expect(matchesScheduleVisibility(expired, filter)).toBe(expiredMatch);
    }
  );
});
