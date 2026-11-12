import { cleanup, render, screen, fireEvent } from '@testing-library/svelte';
import { afterEach, expect, it, vi } from 'vitest';
import { ApiError } from '$lib/api/client';
import { fixtureQuery, fixtureView } from './fixtures';
import type { UsageDataSource } from './api';
import RequestLogPanel from './RequestLogPanel.svelte';
import WorkPanel from './WorkPanel.svelte';
import UsageDashboard from './UsageDashboard.svelte';
import { usageErrorMessage } from './adapter';
import { createUsageDataSource } from './adapter';
import { costSeries } from './chart-data';
import { protocolFixture } from '../../../e2e/usage/protocol-fixture';

afterEach(cleanup);
const failure = new ApiError('The usage database is unavailable. Try again.', {
  status: 503, code: 'usage_query_failed',
  details: { operation: 'Load request log', correlation_id: 'usage-reference-17' }
});
it('preserves operation, reason and reference through the formatter', () => {
  expect(usageErrorMessage(failure)).toBe('Load request log failed: The usage database is unavailable. Try again. · Reference usage-reference-17');
});
it('says when the usage request never reached Cognis', () => {
  expect(usageErrorMessage(new TypeError('Failed to fetch'))).toBe('Load usage failed: Cognis could not be reached. Check the connection and try again.');
});
it('request log shows a request-linked failure and recovery action', async () => {
  const requestLog = vi.fn(async () => { throw failure; });
  render(RequestLogPanel, { query: fixtureQuery, source: { requestLog } as unknown as UsageDataSource });
  expect((await screen.findByText(/Reference usage-reference-17/)).textContent).toContain('Load request log failed');
  expect(screen.getByRole('button', { name: /refresh/i })).toBeTruthy();
  await fireEvent.click(screen.getByRole('button', { name: /refresh/i }));
  expect(requestLog.mock.calls.length).toBeGreaterThanOrEqual(2);
});
it('work ranking preserves safe request-linked errors instead of raw exceptions', async () => {
  const ranking = vi.fn(async () => { throw failure; });
  render(WorkPanel, { query: fixtureQuery, source: { ranking } as unknown as UsageDataSource, onrow: vi.fn() });
  expect((await screen.findByRole('alert')).textContent).toContain('Reference usage-reference-17');
});
it('work ranking does not forward an arbitrary exception body', async () => {
  const ranking = vi.fn(async () => { throw new Error('PRIVATE payload'); });
  render(WorkPanel, { query: fixtureQuery, source: { ranking } as unknown as UsageDataSource, onrow: vi.fn() });
  expect((await screen.findByRole('alert')).textContent).not.toContain('PRIVATE');
});
it('partial data remains visible independently of stale freshness', () => {
  const view = fixtureView(fixtureQuery);
  view.status = 'partial'; view.freshness = '2020-01-01T00:00:00Z';
  render(UsageDashboard, { query: fixtureQuery, result: { state: 'ready', view }, onquery: vi.fn(), onrefresh: vi.fn() });
  expect(screen.getByText('Partial data')).toBeTruthy();
  expect(screen.getByText(/Stale · Updated/)).toBeTruthy();
});
it('stale ready data is labelled stale, not healthy or unknown', () => {
  const view = fixtureView(fixtureQuery);
  view.status = 'ready'; view.freshness = '2020-01-01T00:00:00Z';
  render(UsageDashboard, { query: fixtureQuery, result: { state: 'ready', view }, onquery: vi.fn(), onrefresh: vi.fn() });
  expect(screen.getByText('Stale', { exact: true })).toBeTruthy();
  expect(screen.queryByText('Ready', { exact: true })).toBeNull();
});
it('shows one factual empty cost message rather than repeating implementation text for each dimension', async () => {
  const source = createUsageDataSource(protocolFixture(() => 'empty').send);
  const result = await source.query(fixtureQuery, new AbortController().signal);
  render(UsageDashboard, { query: fixtureQuery, result, source, onquery: vi.fn(), onrefresh: vi.fn() });
  expect(screen.getAllByText('No cost breakdown for this range.')).toHaveLength(1);
  expect(screen.queryByText(/monetary sectors/)).toBeNull();
});
it('cost tooltip copy uses the canonical estimate label, without implementation guarantees', () => {
  const bands = costSeries(fixtureView(fixtureQuery));
  expect(bands.length).toBeGreaterThan(0);
  expect(bands[0].series[0].points[0].population).toContain('estimate');
  expect(bands[0].series[0].points[0].population).not.toMatch(/decimal strings|authoritative|bucket/);
});
