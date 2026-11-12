import { render, screen, fireEvent } from '@testing-library/svelte';
import { describe, expect, it, vi } from 'vitest';
import AttemptDetail from './AttemptDetail.svelte';
import UsageDashboard from './UsageDashboard.svelte';
import { averageMs, throughput, attemptErrorRate } from './model';
import { fixturePerformance } from './performance-fixtures';
import { fixtureAttempts, fixtureQuery } from './fixtures';
import { fixtureScreen } from './screen-fixture';
import type { UsageQuery } from './model';

describe('performance and evidence boundaries', () => {
  it('uses weighted eligible token/duration sums, never a mean of rates', () => {
    expect(throughput({ outputTokens: 300, durationMs: 6000, samples: 2 })).toBe('50.0 tok/s');
    expect(throughput({ outputTokens: 300, durationMs: null, samples: 2 })).toBe('Unknown');
    expect(averageMs({ totalMs: null, samples: 0 })).toBe('Unknown');
    expect(attemptErrorRate({ ...fixturePerformance[0], dispatchedAttempts: 0 })).toBe('Unknown');
  });
  it('keeps output-dependent measurements unknown on a failed attempt with no output', () => {
    expect(fixtureAttempts[1].detail?.timings.serverTtft).toEqual({ totalMs: null, samples: 0 });
    expect(throughput(fixtureAttempts[1].detail!.timings.generation)).toBe('Unknown');
    expect(throughput(fixtureAttempts[1].detail!.timings.endToEnd)).toBe('Unknown');
  });
  it('suppresses client/key identifiers in project attempt detail', () => {
    render(AttemptDetail, { attempt: fixtureAttempts[0], mode: 'project' });
    expect(screen.queryByText('Authorized client identifiers')).toBeNull();
    expect(screen.queryByText('service-key-17')).toBeNull();
    expect(screen.getByText('Session-less request')).toBeTruthy();
    expect(screen.getByText('top_k')).toBeTruthy();
  });
  it('fixes project scope and supports cycle-to-attempt details', async () => {
    const query: UsageQuery = { ...fixtureQuery, surface: 'explorer', explorerMode: 'attempts', mode: 'project', projectScope: { id: 'Cognis', label: 'Cognis' } };
    const onquery = vi.fn();
    const { rerender } = render(UsageDashboard, { query, surface: 'explorer', result: { state: 'ready', view: fixtureScreen(query) }, onquery, onrefresh: vi.fn() });
    expect(screen.queryByRole('combobox', { name: 'Filter project' })).toBeNull();
    await fireEvent.click(screen.getAllByRole('button', { name: 'fixture-cognis-cycle-1' })[0]);
    const cycleQuery: UsageQuery = onquery.mock.lastCall![0];
    expect(cycleQuery).toMatchObject({ cycleId: 'fixture-cognis-cycle-1', mode: 'project', projectScope: query.projectScope });
    const selectedQuery = { ...cycleQuery, selectedAttempt: 'fixture-cognis-request-1' };
    await rerender({ query: selectedQuery, surface: 'explorer', result: { state: 'ready', view: fixtureScreen(selectedQuery) }, onquery, onrefresh: vi.fn() });
    expect(screen.getByRole('region', { name: 'Attempt details' })).toBeTruthy();
    expect(screen.getAllByText(/rate_limit.*429/).length).toBeGreaterThan(0);
    expect(screen.queryByRole('button', { name: 'fixture-cognis-request-3' })).toBeNull();
    expect(screen.queryByText('Authorized client identifiers')).toBeNull();
  });
  it('clears page-local detail selections when cursor continuation replaces the page', async () => {
    const query: UsageQuery = { ...fixtureQuery, surface: 'explorer', explorerMode: 'attempts', selectedAttempt: 'fixture-cognis-request-1' };
    const onquery = vi.fn();
    const props = { query, surface: 'explorer' as const, result: { state: 'ready' as const, view: fixtureScreen(query) }, onquery, onrefresh: vi.fn() };
    const { rerender } = render(UsageDashboard, props);
    expect(screen.getByRole('region', { name: 'Attempt details' })).toBeTruthy();
    await fireEvent.click(screen.getByRole('button', { name: 'Next attempt page' }));
    const nextQuery: UsageQuery = onquery.mock.lastCall![0];
    expect(nextQuery).toMatchObject({ cursor: 'fixture-page-3', selectedAttempt: undefined });
    await rerender({ ...props, query: nextQuery, result: { state: 'ready', view: fixtureScreen(nextQuery) } });
    expect(screen.getByRole('button', { name: 'fixture-cognis-request-4' })).toBeTruthy();
    expect(screen.queryByRole('region', { name: 'Attempt details' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'fixture-cognis-request-1' })).toBeNull();
  });
});
