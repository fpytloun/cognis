import { cleanup, fireEvent, render, screen, within } from '@testing-library/svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '$lib/api/client';
import { createUsageDataSource, type UsageTransport } from './adapter';
import { catalog, metrics, response } from './wire.test-data';
import { fixtureQuery } from './fixtures';
import { costComposition, overviewCostBands } from './composition';
import UsageDashboard from './UsageDashboard.svelte';
import type * as W from './wire';
import { wireQuery } from './query-contract';

afterEach(cleanup);
const query = { ...fixtureQuery, mode: 'personal' as const, surface: 'overview' as const, filters: { ...fixtureQuery.filters, provider: 'selected-provider' } };
function canonical() {
  const value = response();
  value.total.costs = [
    { ...metrics().costs[0], known_amount: '0.300000000000000003', currency: 'USD' },
    { ...metrics().costs[0], known_amount: '1', currency: 'EUR' },
    { ...metrics().costs[0], known_amount: '2', basis: 'configured_equivalent' }
  ];
  value.total.partially_priced_attempts = 1; value.total.unpriced_attempts = 3; value.total.unallocatable_cost_attempts = 1;
  for (const dimension of value.cost_breakdowns!.dimensions) {
    const first = metrics(), second = metrics(), missing = metrics();
    first.costs = [{ ...value.total.costs[0], known_amount: '0.100000000000000001' }, value.total.costs[1]];
    second.costs = [{ ...value.total.costs[0], known_amount: '0.200000000000000002' }, value.total.costs[2]];
    missing.costs = [];
    dimension.groups = [
      { key: `first-${dimension.dimension}`, label: `First ${dimension.dimension}`, metrics: first },
      { key: `second-${dimension.dimension}`, label: `Second ${dimension.dimension}`, metrics: second },
      { key: null, label: 'Unallocated / redacted', metrics: missing }
    ];
  }
  return value;
}
function transport(main = canonical(), trendError?: number) {
  const calls: { path: string; body: Partial<W.Query> }[] = [];
  const send: UsageTransport = async <T>(path: string, options: Parameters<UsageTransport>[1]) => {
    const body = JSON.parse(String(options?.body ?? '{}')) as W.Query; calls.push({ path, body });
    if (path.endsWith('/catalog')) return catalog(body.scope) as T;
    if (body.view === 'cost_breakdowns') return main as T;
    if (trendError) throw new ApiError('Timeline unavailable', { status: trendError });
    const other = response();
    other.meta.scope = body.scope;
    other.meta.observed_through = '2026-10-29T19:00:00Z';
    other.total.costs = [{ ...metrics().costs[0], known_amount: '999', currency: 'GBP' }];
    other.buckets[0].metrics = other.total;
    return other as T;
  };
  return { source: createUsageDataSource(send), calls };
}
describe('live Overview operation composition', () => {
  it('maps all four complete marginals against only their single-operation total and compatible band', async () => {
    const { source, calls } = transport();
    const result = await source.query(query, new AbortController().signal);
    expect(result.state).toBe('ready');
    if (result.state !== 'ready') throw new Error('Expected canonical breakdowns');
    expect(calls.map((call) => call.body.view)).toEqual([undefined, 'cost_breakdowns', 'trend']);
    expect(calls[2].body).toMatchObject({ start: calls[1].body.start, end: calls[1].body.end, scope: calls[1].body.scope, filters: calls[1].body.filters });
    expect(calls[1].body.group_by).toBeUndefined();
    expect(calls[2].body.group_by).toBeUndefined();
    expect(result.view.totals?.costs?.[0].amount).toBe('0.300000000000000003');
    expect(overviewCostBands(result.view).map((band) => band.key)).toEqual(['USD:api_estimate', 'EUR:api_estimate', 'USD:api_equivalent']);
    const band = result.view.costBreakdowns![0];
    expect(band.coverage).toMatchObject({ partial: 1, unpriced: 3, unallocatable: 1 });
    for (const dimension of ['user', 'agent', 'provider', 'model'] as const) {
      const composition = costComposition(band, dimension);
      expect(composition.reason).toBeNull();
      expect(composition.total).toBe('$0.30');
      expect(composition.sectors).toHaveLength(2);
      expect(composition.rows.find((row) => row.cohort.amount === null)?.cohort.drill).toBeNull();
    }
    expect(band.dimensions.user.state === 'ready' && band.dimensions.user.groups[0].drill).toEqual({ detailFilters: { subject_keys: ['first-subject'] } });
  });
  it('renders four simultaneous exact-value panels via the real adapter and binds subject drill without email inference', async () => {
    const { source } = transport(), onquery = vi.fn();
    const result = await source.query(query, new AbortController().signal);
    render(UsageDashboard, { query, result, surface: 'overview', onquery, onrefresh: vi.fn() });
    for (const title of ['User', 'Agent', 'Model']) {
      expect(screen.getByRole('region', { name: `${title} cost attribution` })).toBeTruthy();
    }
    // Provider share is the donut beside the trend.
    expect(screen.getByRole('region', { name: 'Cost share by provider' })).toBeTruthy();
    const user = screen.getByRole('region', { name: 'User cost attribution' });
    expect(within(user).getByTitle('$0.10').textContent).toBe('$0.10');
    // Rounded money in the accessible name of every ranked row and the headline tooltip.
    expect(within(user).getByRole('button', { name: /\$0\.10/ })).toBeTruthy();
    expect(screen.getAllByTitle('$0.30 USD · estimated').length).toBeGreaterThan(0);
    await fireEvent.click(within(user).getByRole('button', { name: /First subject:.*Inspect filtered requests/ }));
    expect(onquery.mock.lastCall![0]).toMatchObject({ mode: 'personal', filters: query.filters, detailFilters: { subject_keys: ['first-subject'] } });
    expect(onquery.mock.lastCall![0].filters.user).toBe(query.filters.user);
  });
  it('refuses malformed or incomplete fixed sets without ranking or a fallback query', async () => {
    for (const broken of ['missing', 'incomplete', 'duplicate'] as const) {
      const main = canonical();
      if (broken === 'missing') main.cost_breakdowns = null;
      if (broken === 'incomplete') (main.cost_breakdowns as unknown as { complete_groups: boolean }).complete_groups = false;
      if (broken === 'duplicate') main.cost_breakdowns!.dimensions[1] = main.cost_breakdowns!.dimensions[0];
      const { source, calls } = transport(main);
      expect((await source.query(query, new AbortController().signal)).state).toBe('error');
      expect(calls.map((call) => call.body.view)).toEqual([undefined, 'cost_breakdowns']);
    }
  });
  it('retains independent marginals on timeline server failure but clears them on authority rejection', async () => {
    const unavailable = await transport(canonical(), 503).source.query(query, new AbortController().signal);
    expect(unavailable.state).toBe('ready');
    if (unavailable.state === 'ready') expect(unavailable.view.buckets).toEqual([]);
    expect((await transport(canonical(), 403).source.query(query, new AbortController().signal)).state).toBe('error');
  });
  it('keeps redacted project marginals chartable but not drillable and preserves fixed scope in both operations', async () => {
    const main = canonical();
    const project = { ...query, mode: 'project' as const, projectScope: { id: 'fixed-project', label: 'Fixed project' } };
    main.meta.scope = { mode: 'project', project_id: 'fixed-project' };
    for (const dimension of main.cost_breakdowns!.dimensions) dimension.groups = [{ key: null, label: 'Authorized project aggregate', metrics: main.total }];
    const { source, calls } = transport(main);
    const result = await source.query(project, new AbortController().signal);
    expect(result.state).toBe('ready');
    if (result.state !== 'ready') throw new Error('Expected scoped marginals');
    for (const marginal of Object.values(result.view.costBreakdowns![0].dimensions)) {
      expect(marginal.state === 'ready' && marginal.groups[0].drill).toBeNull();
    }
    for (const call of calls) expect(call.body.scope).toEqual({ mode: 'project', project_id: 'fixed-project' });
    expect(calls[2].body.filters).toEqual(calls[1].body.filters);
  });
  it('shows scope-wide missing pricing without inventing a zero band or adopting independently observed trend bands', async () => {
    const main = canonical(); main.total.costs = [];
    for (const dimension of main.cost_breakdowns!.dimensions) for (const group of dimension.groups) group.metrics.costs = [];
    const { source } = transport(main);
    const result = await source.query(query, new AbortController().signal);
    expect(result.state).toBe('ready');
    if (result.state !== 'ready') throw new Error('Expected explicit missing pricing');
    expect(result.view.costBreakdowns).toEqual([]);
    expect(overviewCostBands(result.view)).toEqual([]);
    const { container } = render(UsageDashboard, { query, result, surface: 'overview', onquery: vi.fn(), onrefresh: vi.fn() });
    expect(container.querySelector('svg[role="group"]')).toBeNull();
    const coverage = screen.getByRole('group', { name: 'Pricing coverage' });
    expect(coverage.textContent).toMatch(/3 not fully priced/);
    expect(coverage.textContent).toMatch(/1 price unknown/);
  });
  it('filters by a provider from the donut, then drills an agent without losing the selected population', async () => {
    const base = { ...query, filters: { ...query.filters, provider: '' }, preciseStart: '2026-10-01T12:34:56Z', preciseEnd: '2026-10-02T15:16:17Z' };
    const { source } = transport(), onquery = vi.fn();
    const result = await source.query(base, new AbortController().signal);
    const { rerender } = render(UsageDashboard, { query: base, result, surface: 'overview', onquery, onrefresh: vi.fn() });
    const provider = screen.getByRole('region', { name: 'Cost share by provider' });
    await fireEvent.click(within(provider).getByRole('button', { name: 'First provider' }));
    const first = onquery.mock.lastCall![0];
    // The donut narrows the dashboard (a visible filter chip), it does not leave for requests.
    expect(first).toMatchObject({ filters: { provider: 'first-provider' }, preciseStart: base.preciseStart, preciseEnd: base.preciseEnd });
    expect(first.detailFilters).toBeUndefined();
    const returned = { ...first, surface: 'overview' as const };
    const narrowed = await source.query(returned, new AbortController().signal);
    await rerender({ query: returned, result: narrowed, surface: 'overview', onquery, onrefresh: vi.fn() });
    const agent = screen.getByRole('region', { name: 'Agent cost attribution' });
    await fireEvent.click(within(agent).getByRole('button', { name: /First agent:.*Inspect filtered requests/ }));
    const second = onquery.mock.lastCall![0];
    expect(second.detailFilters).toEqual({ agent_ids: ['first-agent'] });
    expect(second).toMatchObject({ mode: base.mode, filters: { ...base.filters, provider: 'first-provider' }, preciseStart: base.preciseStart, preciseEnd: base.preciseEnd });
    expect(wireQuery(second)).toMatchObject({ start: base.preciseStart.replace('Z', '.000Z'), end: base.preciseEnd.replace('Z', '.000Z'),
      scope: { mode: 'personal' }, filters: { provider_ids: ['first-provider'], agent_ids: ['first-agent'] } });
  });
  it('refuses a disjoint marginal drill instead of replacing existing predicates with a broader population', async () => {
    const base = { ...query, filters: { ...query.filters, provider: '' }, detailFilters: { agent_ids: ['other-agent'], provider_ids: ['kept-provider'] } };
    const { source } = transport(), onquery = vi.fn();
    const result = await source.query(base, new AbortController().signal);
    render(UsageDashboard, { query: base, result, surface: 'overview', onquery, onrefresh: vi.fn() });
    const agent = screen.getByRole('region', { name: 'Agent cost attribution' });
    await fireEvent.click(within(agent).getByRole('button', { name: /First agent:.*Inspect filtered requests/ }));
    expect(onquery).not.toHaveBeenCalled();
    expect(screen.getByRole('status').textContent).toContain('do not intersect');
  });
});
