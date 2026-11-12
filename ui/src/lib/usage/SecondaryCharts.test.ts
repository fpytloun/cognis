import { cleanup, fireEvent, render, screen, within } from '@testing-library/svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import UsageDashboard from './UsageDashboard.svelte';
import { fixtureQuery } from './fixtures';
import { fixtureScreen, fixtureDayPerformance } from './screen-fixture';

afterEach(cleanup);
describe('charts-led supported usage views', () => {
  it('leads Costs with a cost trend, token and cache charts and a sortable breakdown', async () => {
    const query = { ...fixtureQuery, surface: 'costs' as const, groupBy: 'agent' as const };
    const view = fixtureScreen(query);
    view.groups = [
      { ...view.totals!, label: 'Cheap agent', id: 'cheap', cost: { amount: '1', currency: 'USD', basis: 'api_estimate' }, costs: [{ amount: '1', currency: 'USD', basis: 'api_estimate' }] },
      { ...view.totals!, label: 'Costly agent', id: 'costly', cost: { amount: '9', currency: 'USD', basis: 'api_estimate' }, costs: [{ amount: '9', currency: 'USD', basis: 'api_estimate' }] }
    ];
    const onquery = vi.fn();
    const { container } = render(UsageDashboard, { query, surface: 'costs', result: { state: 'ready', view }, onquery, onrefresh: vi.fn() });
    const headings = [...container.querySelectorAll('h2')].map((element) => element.textContent);
    expect(headings.indexOf('Estimated cost per day')).toBeLessThan(headings.indexOf('Tokens per day'));
    expect(screen.getByRole('region', { name: 'Cache reads over time' })).toBeTruthy();
    const table = screen.getByRole('region', { name: 'Cost by agent' });
    // Sorted by estimated cost, highest first.
    expect(within(table).getAllByRole('rowheader').map((cell) => cell.textContent?.trim())).toEqual(['Costly agent', 'Cheap agent']);
    await fireEvent.click(within(table).getByRole('button', { name: /Est. cost/ }));
    expect(within(table).getAllByRole('rowheader').map((cell) => cell.textContent?.trim())).toEqual(['Cheap agent', 'Costly agent']);
    await fireEvent.click(screen.getByRole('radio', { name: 'Model' }));
    expect(onquery.mock.lastCall![0]).toMatchObject({ groupBy: 'model', filters: query.filters });
    await fireEvent.click(within(table).getByRole('button', { name: 'Costly agent' }));
    expect(onquery.mock.lastCall![0]).toMatchObject({ surface: 'explorer', filters: { ...query.filters, agent: 'costly' } });
  });
  it('summarises pooled performance, groups it and drills a row without changing filter scope', async () => {
    const query = { ...fixtureQuery, surface: 'performance' as const, groupBy: 'provider' as const, filters: { ...fixtureQuery.filters, effort: 'high' } };
    const view = fixtureScreen(query);
    view.groups = [{ ...view.totals!, label: 'Synthetic provider', id: 'synthetic-provider', performance: fixtureDayPerformance(0) }];
    const onquery = vi.fn();
    render(UsageDashboard, { query, surface: 'performance', result: { state: 'ready', view }, onquery, onrefresh: vi.fn() });
    const summary = screen.getByRole('region', { name: 'Performance summary' });
    // Pooled sum/count over the fixture buckets, not a mean of daily means.
    expect(summary.textContent).toMatch(/Avg request time\s*\d+(\.\d)? s/);
    expect(summary.textContent).toMatch(/Failure rate\s*\d+%/);
    expect(screen.getByRole('region', { name: 'Latency per day' })).toBeTruthy();
    const table = screen.getByRole('region', { name: 'Performance by provider' });
    await fireEvent.click(within(table).getByRole('button', { name: 'Synthetic provider' }));
    expect(onquery.mock.lastCall![0]).toMatchObject({ mode: query.mode, filters: { ...query.filters, provider: 'synthetic-provider' }, surface: 'explorer' });
    await fireEvent.click(screen.getByRole('radio', { name: 'Model' }));
    expect(onquery.mock.lastCall![0]).toMatchObject({ groupBy: 'model', surface: 'performance' });
  });
  it('lists companion services and narrows to one without inventing an overall trend', async () => {
    const query = { ...fixtureQuery, surface: 'companions' as const };
    const view = fixtureScreen(query);
    view.groups = [{ ...view.totals!, label: 'Synthetic memory', id: 'synthetic-memory', performance: fixtureDayPerformance(0) }];
    const onquery = vi.fn();
    const { rerender } = render(UsageDashboard, { query, surface: 'companions', result: { state: 'ready', view }, onquery, onrefresh: vi.fn() });
    expect(screen.queryByRole('region', { name: 'Service requests over time' })).toBeNull();
    await fireEvent.click(screen.getByRole('button', { name: 'Synthetic memory' }));
    const narrowed = onquery.mock.lastCall![0];
    expect(narrowed).toMatchObject({ serviceId: 'synthetic-memory', operation: undefined, filters: query.filters });
    await rerender({ query: narrowed, surface: 'companions', result: { state: 'ready', view }, onquery, onrefresh: vi.fn() });
    expect(screen.getByRole('region', { name: 'Service requests over time' })).toBeTruthy();
    await fireEvent.click(screen.getByRole('button', { name: 'All services' }));
    expect(onquery.mock.lastCall![0]).toMatchObject({ serviceId: undefined });
  });
  it('keeps untagged service traffic visible without a drill', () => {
    const query = { ...fixtureQuery, surface: 'companions' as const };
    const view = fixtureScreen(query);
    view.groups = [{ ...view.totals!, label: 'Unallocated group', id: null, performance: fixtureDayPerformance(0) }];
    render(UsageDashboard, { query, surface: 'companions', result: { state: 'ready', view }, onquery: vi.fn(), onrefresh: vi.fn() });
    expect(screen.getByText('Unallocated group')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Unallocated group' })).toBeNull();
  });
  it('shows the session page as a table and keeps the protected session drill', async () => {
    const query = { ...fixtureQuery, surface: 'explorer' as const, explorerMode: 'sessions' as const };
    const view = fixtureScreen(query), onquery = vi.fn();
    render(UsageDashboard, { query, surface: 'explorer', result: { state: 'ready', view }, onquery, onrefresh: vi.fn() });
    await fireEvent.click(screen.getByRole('button', { name: view.sessions[0].label }));
    expect(onquery.mock.lastCall![0]).toMatchObject({ selectionRef: view.sessions[0].selectionRef, requiresSelection: true, detailFilters: view.sessions[0].drillFilters });
    expect(onquery.mock.lastCall![0].sessionId).toBeUndefined();
  });
});
