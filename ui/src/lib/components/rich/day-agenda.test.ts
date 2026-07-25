import { describe, expect, it } from 'vitest';
import { normalizeDayAgenda } from './day-agenda';

describe('normalizeDayAgenda', () => {
  it('filters malformed values and sorts unsorted overlapping events deterministically', () => {
    const agenda = normalizeDayAgenda({
      type: 'day_agenda',
      timezone: 'Europe/Prague',
      now: '2026-10-25T02:15:00+02:00',
      items: [
        null, 'invalid', { title: null }, { title: 'Invalid time', start: '09:00' },
        { title: 'Later', start: '2026-10-25T03:00:00+01:00', end: '2026-10-25T04:00:00+01:00' },
        { title: 'Current B', start: '2026-10-25T02:00:00+02:00', end: '2026-10-25T02:30:00+02:00' },
        { title: 'Current A', start: '2026-10-25T02:00:00+02:00', end: '2026-10-25T02:30:00+02:00' },
        { title: 'All day', all_day: true },
        { title: 'Backwards', start: '2026-10-25T05:00:00+01:00', end: '2026-10-25T04:00:00+01:00' },
      ],
      tasks: [null, 1, { content: 'Task alias' }, { title: '' }],
    });
    expect(agenda.allDay.map((item) => item.title)).toEqual(['All day']);
    expect(agenda.timed.map((item) => item.title)).toEqual([
      'Current A', 'Current B', 'Later', 'Backwards',
    ]);
    expect(agenda.timed[0].state).toBe('current');
    expect(agenda.timed[0].isNext).toBe(true);
    expect(agenda.timed[3].end).toBeNull();
    expect(agenda.markerIndex).toBe(2);
    expect(agenda.tasks.map((task) => task.title)).toEqual(['Task alias']);
  });

  it('places now before all-future and after all-past schedules', () => {
    const future = normalizeDayAgenda({
      type: 'day_agenda',
      now: '2026-07-12T07:00:00+02:00',
      items: [{ title: 'Future', start: '2026-07-12T09:00:00+02:00' }],
    });
    const past = normalizeDayAgenda({
      type: 'day_agenda',
      now: '2026-07-12T23:00:00+02:00',
      items: [{ title: 'Past', start: '2026-07-12T09:00:00+02:00', end: '2026-07-12T10:00:00+02:00' }],
    });
    expect(future.markerIndex).toBe(0);
    expect(future.timed[0].state).toBe('future');
    expect(past.markerIndex).toBe(1);
    expect(past.timed[0].state).toBe('past');
  });

  it('orders cross-midnight and DST instants by absolute chronology', () => {
    const agenda = normalizeDayAgenda({
      type: 'day_agenda',
      timezone: 'Europe/Prague',
      now: '2026-03-29T01:45:00+01:00',
      items: [
        { title: 'After DST jump', start: '2026-03-29T03:15:00+02:00' },
        { title: 'Before midnight', start: '2026-03-28T23:30:00+01:00', end: '2026-03-29T00:30:00+01:00' },
        { title: 'Current cross-midnight', start: '2026-03-29T00:30:00+01:00', end: '2026-03-29T03:30:00+02:00' },
      ],
    });
    expect(agenda.timed.map((item) => item.title)).toEqual([
      'Before midnight', 'Current cross-midnight', 'After DST jump',
    ]);
    expect(agenda.timed[1].state).toBe('current');
    expect(agenda.timed[1].isNext).toBe(true);
  });

  it('never computes a marker index when there are zero timed items, even with a valid now', () => {
    // `markerIndex = timed.filter(...).length` would previously be `0` (not
    // null) here since `now` is set but `timed` is empty -- and
    // DayAgendaBlock.svelte renders a standalone "current time" marker
    // whenever `markerIndex === timed.length`, which is trivially true for
    // an empty list (0 === 0). That rendered a redundant second "now" line
    // directly under the header's own current-time display, with nothing
    // to anchor it against, and matches the exact same
    // `marker_index == len(timed_items)` zero-items case fixed in the
    // Python renderer's `_render_day_agenda`.
    const agenda = normalizeDayAgenda({
      type: 'day_agenda',
      timezone: 'Europe/Prague',
      now: '2026-07-17T08:00:00+02:00',
      items: [],
    });

    expect(agenda.timed).toEqual([]);
    expect(agenda.markerIndex).toBeNull();
  });

  it('rejects calendar overflows, invalid wall times, and malformed offsets', () => {
    const agenda = normalizeDayAgenda({
      type: 'day_agenda',
      now: '2026-02-30T09:00:00+01:00',
      items: [
        { title: 'Overflow date', start: '2026-02-30T10:00:00+01:00' },
        { title: 'Overflow hour', start: '2026-02-28T24:00:00+01:00' },
        { title: 'Bad offset', start: '2026-02-28T10:00:00+24:00' },
        { title: 'Valid', start: '2026-02-28T10:00:00+01:00' },
      ],
    });
    expect(agenda.now).toBeNull();
    expect(agenda.timed.map((item) => item.title)).toEqual(['Valid']);
    expect(agenda.timed[0].state).toBe('neutral');
    expect(agenda.timed[0].isNext).toBe(false);
    expect(agenda.markerIndex).toBeNull();
  });

  it('treats missing end as zero-duration and gives canonical fields precedence', () => {
    const agenda = normalizeDayAgenda({
      type: 'day_agenda',
      timezone: 'Invalid/Timezone',
      now: '2026-07-12T09:00:00Z',
      now_iso: '2026-07-12T08:00:00Z',
      items: [
        {
          title: 'Instant event',
          start: '2026-07-12T09:00:00Z',
          end: '2026-07-12T09:00:00Z',
          start_iso: '2026-07-12T10:00:00Z',
        },
        { title: 'Future event', start_iso: '2026-07-12T10:00:00Z' },
      ],
    });
    expect(agenda.timezone).toBe('UTC');
    expect(agenda.timed.map((item) => item.title)).toEqual(['Instant event', 'Future event']);
    expect(agenda.timed[0].end).toBeNull();
    expect(agenda.timed[0].state).toBe('past');
    expect(agenda.timed[1].isNext).toBe(true);
  });

  it.each([null, false, '', {}])(
    'does not consult events when canonical items is present as %p',
    (items) => {
      const agenda = normalizeDayAgenda({
        type: 'day_agenda',
        items,
        events: [{ title: 'Must not leak', all_day: true }],
      });
      expect(agenda.allDay).toEqual([]);
      expect(agenda.timed).toEqual([]);
    },
  );

  it('uses presence precedence for null, false, and empty canonical item fields', () => {
    const agenda = normalizeDayAgenda({
      type: 'day_agenda',
      now: null,
      now_iso: '2026-07-12T09:00:00Z',
      items: [
        { title: null, label: 'Hidden', all_day: true },
        {
          title: 'Timed', all_day: false, allDay: true,
          start: '', start_iso: '2026-07-12T10:00:00Z',
        },
      ],
      tasks: [{ title: '', content: 'Hidden task' }],
    });
    expect(agenda.now).toBeNull();
    expect(agenda.allDay).toEqual([]);
    expect(agenda.timed).toEqual([]);
    expect(agenda.tasks).toEqual([]);
  });

  it('orders opposite fall-back folds and evaluates overlap by epoch', () => {
    const agenda = normalizeDayAgenda({
      type: 'day_agenda',
      timezone: 'Europe/Prague',
      now: '2026-10-25T02:15:00+01:00',
      items: [
        {
          title: 'Second fold current',
          start: '2026-10-25T02:00:00+01:00',
          end: '2026-10-25T02:30:00+01:00',
        },
        {
          title: 'First fold',
          start: '2026-10-25T02:30:00+02:00',
          end: '2026-10-25T02:45:00+02:00',
        },
      ],
    });
    expect(agenda.timed.map((item) => item.title)).toEqual(['First fold', 'Second fold current']);
    expect(agenda.timed.map((item) => item.state)).toEqual(['past', 'current']);
    expect(agenda.markerIndex).toBe(2);
  });

  it('prefers canonical source provenance, sanitizes its URL, and only falls back when absent', () => {
    const canonical = normalizeDayAgenda({
      type: 'day_agenda',
      freshness: 'legacy must not leak',
      source: {
        label: 'Google Calendar',
        url: 'javascript:alert(1)',
        refreshed_at: '07:10 CEST',
      },
    });
    const fallback = normalizeDayAgenda({ type: 'day_agenda', freshness: '07:05 CEST' });

    expect(canonical.source).toEqual({
      label: 'Google Calendar',
      url: '',
      refreshedAt: '07:10 CEST',
    });
    expect(fallback.source).toEqual({
      label: 'Calendar and tasks',
      url: '',
      refreshedAt: '07:05 CEST',
    });
  });
});
