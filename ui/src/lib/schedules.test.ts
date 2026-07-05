import { describe, expect, it } from 'vitest';

import {
  matchesScheduleFilters,
  matchesScheduleVisibility,
  type ScheduleVisibilityFilter
} from './schedules';

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

describe('matchesScheduleFilters', () => {
  const schedule = {
    name: 'Weekly report',
    project_id: 'project-1',
    schedule_type: 'cron',
    enabled: true,
    is_expired: false
  };

  it('matches schedules against search, project, type, enabled, and visibility filters', () => {
    expect(
      matchesScheduleFilters(schedule, {
        search: 'weekly',
        projectId: 'project-1',
        scheduleType: 'cron',
        enabled: 'enabled',
        visibility: 'active'
      })
    ).toBe(true);
    expect(
      matchesScheduleFilters(schedule, {
        search: '',
        projectId: 'project-2',
        scheduleType: '',
        enabled: '',
        visibility: 'all'
      })
    ).toBe(false);
    expect(
      matchesScheduleFilters(schedule, {
        search: '',
        projectId: '',
        scheduleType: 'interval',
        enabled: '',
        visibility: 'all'
      })
    ).toBe(false);
    expect(
      matchesScheduleFilters(schedule, {
        search: '',
        projectId: '',
        scheduleType: '',
        enabled: 'disabled',
        visibility: 'all'
      })
    ).toBe(false);
    expect(
      matchesScheduleFilters(schedule, {
        search: '',
        projectId: '',
        scheduleType: '',
        enabled: '',
        visibility: 'expired'
      })
    ).toBe(false);
  });
});
