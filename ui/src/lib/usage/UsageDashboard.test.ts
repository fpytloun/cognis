import { cleanup, fireEvent, render, screen, within } from '@testing-library/svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import UsageDashboard from './UsageDashboard.svelte';
import { fixtureQuery } from './fixtures';
import { fixtureScreen } from './screen-fixture';
import { compositionBand } from './composition.test-data';
afterEach(cleanup);
describe('focused Usage workspace', () => {
  it('does not label provider-reported chart amounts as estimates', () => {
    const view = fixtureScreen(fixtureQuery);
    view.costBreakdowns = [compositionBand('USD', 'provider_reported')];
    render(UsageDashboard, { query: fixtureQuery, result: { state: 'ready', view }, onquery: vi.fn(), onrefresh: vi.fn() });
    expect(screen.getByRole('heading', { name: 'Cost per day' })).toBeTruthy();
    const providers = screen.getByRole('region', { name: 'Cost share by provider' });
    expect(within(providers).getByText('reported')).toBeTruthy();
    expect(within(providers).getByRole('img', { name: /^Cost by provider:/ })).toBeTruthy();
    expect(within(providers).queryByText('estimated')).toBeNull();
  });
  it('emits typed filter changes without fetching or discarding other filters', async () => {
    const onquery = vi.fn();
    render(UsageDashboard, { query: fixtureQuery, result: { state: 'ready', view: fixtureScreen(fixtureQuery) }, onquery, onrefresh: vi.fn() });
    await fireEvent.change(screen.getByRole('combobox', { name: 'Filter provider' }), { target: { value: 'OpenAI' } });
    expect(onquery).toHaveBeenCalledWith({ ...fixtureQuery, filters: { ...fixtureQuery.filters, provider: 'OpenAI' } });
    // Facts only: no explanatory disclaimers on the page.
    expect(screen.queryByText(/not an invoice|not a cash invoice|best-effort|not guaranteed/i)).toBeNull();
    expect(screen.queryByRole('heading', { name: 'Token consumption over time' })).toBeNull();
    expect(screen.getByRole('heading', { name: 'Estimated cost per day' })).toBeTruthy();
    for (const label of ['User', 'Agent', 'Model']) {
      expect(screen.getByRole('region', { name: `${label} cost attribution` })).toBeTruthy();
    }
    expect(screen.getByRole('region', { name: 'Cost share by provider' })).toBeTruthy();
  });
  it('applies a date preset as one typed range change', async () => {
    const onquery = vi.fn();
    render(UsageDashboard, { query: fixtureQuery, result: { state: 'ready', view: fixtureScreen(fixtureQuery) }, onquery, onrefresh: vi.fn() });
    await fireEvent.click(screen.getByRole('radio', { name: '7 days' }));
    const { start, end } = onquery.mock.calls[0][0].filters;
    expect((Date.parse(end) - Date.parse(start)) / 86_400_000).toBe(6);
    expect(onquery.mock.calls[0][0].filters.provider).toBe(fixtureQuery.filters.provider);
  });
  it('switches the trend between cost and tokens without fetching', async () => {
    const onquery = vi.fn();
    render(UsageDashboard, { query: fixtureQuery, result: { state: 'ready', view: fixtureScreen(fixtureQuery) }, onquery, onrefresh: vi.fn() });
    await fireEvent.click(screen.getByRole('radio', { name: 'Tokens' }));
    expect(screen.getByRole('heading', { name: 'Tokens per day' })).toBeTruthy();
    expect(screen.getAllByRole('button', { name: /Fresh input .* Cached input .* Output/ }).length).toBeGreaterThan(0);
    expect(onquery).not.toHaveBeenCalled();
  });
  it('shares one selected currency/basis across four compositions and preserves scoped drills', async () => {
    const onquery = vi.fn(), view = fixtureScreen(fixtureQuery);
    view.costBreakdowns = [compositionBand(), compositionBand('EUR'), compositionBand('USD', 'api_equivalent')];
    render(UsageDashboard, { query: fixtureQuery, result: { state: 'ready', view }, onquery, onrefresh: vi.fn() });
    const headline = screen.getByRole('heading', { name: 'Estimated cost per day' });
    const summary = screen.getByRole('region', { name: 'Usage summary' });
    expect(summary.compareDocumentPosition(headline) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(summary.textContent).toMatch(/fresh .* cached .* out/);
    await fireEvent.change(screen.getByRole('combobox', { name: 'Overview cost band' }), { target: { value: 'EUR:api_estimate' } });
    expect(screen.getAllByTitle('€10.00 EUR · estimated').length).toBeGreaterThan(0);
    // Presentation-only band selection must not dispatch a broader query.
    expect(onquery).not.toHaveBeenCalled();
    await fireEvent.change(screen.getByRole('combobox', { name: 'Overview cost band' }), { target: { value: 'USD:api_equivalent' } });
    expect(screen.getAllByRole('button', { name: /agent Alpha: \$7\.50, 75%/ })).toHaveLength(1);
    await fireEvent.click(screen.getByRole('button', { name: /agent Alpha:.*Inspect filtered requests/ }));
    expect(onquery.mock.calls[0][0]).toMatchObject({
      mode: fixtureQuery.mode, surface: 'explorer', metric: 'known_priced_subtotal',
      filters: { ...fixtureQuery.filters, agent: 'synthetic-agent-alpha' }
    });
    onquery.mockClear();
    await fireEvent.click(screen.getAllByRole('button', { name: /Unallocated \/ redacted:.*Explain unavailable detail/ })[0]);
    expect(onquery).not.toHaveBeenCalled();
    expect(screen.getByRole('status').textContent).toContain('This group has no request filter.');
  });
  it('opens a canonical session as scoped request detail without inventing transcript access', async () => {
    const query = { ...fixtureQuery, surface: 'explorer' as const, explorerMode: 'sessions' as const };
    const onquery = vi.fn();
    render(UsageDashboard, { query, result: { state: 'ready', view: fixtureScreen(query) }, surface: 'explorer', onquery, onrefresh: vi.fn() });
    await fireEvent.click(screen.getByRole('button', { name: 'Cognis · routing investigation' }));
    expect(onquery.mock.calls[0][0]).toMatchObject({ mode: query.mode, surface: 'explorer', explorerMode: 'requests', selectionRef: 'v2.screen-fixture-session-1', sessionId: undefined, detailFilters: { session_ids: ['fixture-cognis-session-1'] }, filters: query.filters });
    expect(screen.queryByRole('link', { name: /transcript/i })).toBeNull();
    expect(screen.getAllByText(/API client|Cognis session/).length).toBeGreaterThan(0);
  });
  it('has seven usable views and keeps unknown/partial/error recovery explicit', async () => {
    const onquery = vi.fn(), onrefresh = vi.fn();
    const { rerender } = render(UsageDashboard, { query: fixtureQuery, result: { state: 'ready', view: fixtureScreen(fixtureQuery) }, onquery, onrefresh });
    const navigation = screen.getByRole('tablist', { name: 'Usage views' });
    expect(screen.getAllByRole('tab').map((tab) => tab.textContent?.trim())).toEqual(['Overview', 'Costs & tokens', 'Performance', 'Forecast', 'Work', 'Requests', 'Services', 'Limits']);
    const filters = screen.getByRole('region', { name: 'Usage filters' });
    expect(navigation.compareDocumentPosition(filters) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.getByRole('region', { name: 'Usage summary' })).toBeTruthy();
    await rerender({ query: fixtureQuery, result: { state: 'error', message: 'Unsupported retained filters' }, onquery, onrefresh });
    expect(screen.getByRole('alert')).toBeTruthy();
    expect(screen.getByRole('combobox', { name: 'Filter provider' })).toBeTruthy();
    await fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
    expect(onrefresh).toHaveBeenCalledTimes(1);
    await fireEvent.click(screen.getByRole('button', { name: 'Clear filters' }));
    expect(onquery.mock.calls.at(-1)![0]).toMatchObject({ filters: { model: '', effort: '' } });
  });
});
