import type { Schedule } from '$lib/types/api';

export type ScheduleVisibilityFilter = 'active' | 'expired' | 'all';

export function matchesScheduleVisibility(
  schedule: Pick<Schedule, 'is_expired'>,
  filter: ScheduleVisibilityFilter
): boolean {
  if (filter === 'all') return true;
  return filter === 'expired' ? schedule.is_expired : !schedule.is_expired;
}
