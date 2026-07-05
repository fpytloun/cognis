import type { Schedule } from '$lib/types/api';

export type ScheduleVisibilityFilter = 'active' | 'expired' | 'all';

export type ScheduleListFilters = {
  search: string;
  projectId: string;
  scheduleType: string;
  enabled: string;
  visibility: ScheduleVisibilityFilter;
};

export function matchesScheduleVisibility(
  schedule: Pick<Schedule, 'is_expired'>,
  filter: ScheduleVisibilityFilter
): boolean {
  if (filter === 'all') return true;
  return filter === 'expired' ? schedule.is_expired : !schedule.is_expired;
}

export function matchesScheduleFilters(
  schedule: Pick<Schedule, 'name' | 'project_id' | 'schedule_type' | 'enabled' | 'is_expired'>,
  filters: ScheduleListFilters
): boolean {
  if (filters.search && !schedule.name.toLowerCase().includes(filters.search.toLowerCase())) {
    return false;
  }
  if (!matchesScheduleVisibility(schedule, filters.visibility)) return false;
  if (filters.projectId && schedule.project_id !== filters.projectId) return false;
  if (filters.scheduleType && schedule.schedule_type !== filters.scheduleType) return false;
  if (filters.enabled === 'enabled' && !schedule.enabled) return false;
  if (filters.enabled === 'disabled' && schedule.enabled) return false;
  return true;
}
