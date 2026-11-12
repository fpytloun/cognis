import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { UsageDataSource } from './api';
import { fixtureQuery } from './fixtures';
import RequestLogPanel from './RequestLogPanel.svelte';
import type * as W from './wire';

afterEach(cleanup);

function row(overrides: Partial<W.RequestLog> = {}): W.RequestLog {
  return {
    request_id: 'req_1', cycle_id: null, session_type: 'proxy', selection_ref: 'sel_1', logical_request_started_at: null,
    started_at: '2026-11-04T10:00:00Z', attempt_id: null, state: 'succeeded', logical_outcome: 'succeeded', timing: {} as W.Request['timing'], drill_filters: {},
    traffic_origin: 'api', auth_type: 'api_key', completed_at: null, requested_model: 'gpt-x', provider_ids: ['openai'], resolved_models: ['gpt-x-2026'],
    request_path: '/v1/chat/completions', streaming: true, http_status_code: 200, finish_reason: 'stop', service_id: null,
    error_category: null, error_summary: null, latency_seconds: 1.25, ttft_seconds: 0.3,
    tokens: { input: 1200, output: 80, cache_read: 1000, cache_write: null, reasoning: null }, usage_quality: 'complete',
    costs: [{ known_amount: '0.0123', currency: 'USD', basis: 'api_estimate', quality: 'complete', priced_attempts: 1, unpriced_attempts: 0 }],
    unpriced_attempts: 0, attempt_count: 2, retry_count: 1, fallback_count: 0,
    client_id: 'key_abc', key_id: 'key_abc', user_agent: 'my-tool/1.4 (linux)', app_name: 'my-tool', app_version: '1.4', client_labels: { session_id: 'run-7' },
    ...overrides
  } as W.RequestLog;
}

function source(rows: W.RequestLog[]) {
  const requestLog = vi.fn<NonNullable<UsageDataSource['requestLog']>>(async () => ({ items: rows, nextCursor: null }));
  const requestDetail = vi.fn<NonNullable<UsageDataSource['requestDetail']>>(async () => ({ request: rows[0], attempts: [] }));
  return { requestLog, requestDetail, value: { load: vi.fn(), requestLog, requestDetail } as unknown as UsageDataSource };
}

describe('request log', () => {
  it('lists one row per request with caller, status, tokens, cost and timing', async () => {
    const { value } = source([row(), row({ request_id: 'req_2', selection_ref: 'sel_2', traffic_origin: 'cognis', auth_type: null, service_id: null, state: 'failed', http_status_code: 429, error_category: 'rate_limit' })]);
    render(RequestLogPanel, { query: { ...fixtureQuery, mode: 'personal' }, source: value });
    const table = await screen.findByRole('table', { name: 'Requests' });
    const rows = within(table).getAllByRole('row').slice(1);
    expect(rows[0].textContent).toMatch(/200.*gpt-x-2026.*my-tool 1\.4.*API · API key.*1\.2K → 80.*\$0\.01.*1\.3 s.*300 ms/);
    expect(rows[0].textContent).toContain('↻1');
    expect(rows[1].textContent).toMatch(/429.*Cognis/);
  });

  it('filters API clients, auth type and errors server-side', async () => {
    const { value, requestLog } = source([row()]);
    render(RequestLogPanel, { query: fixtureQuery, source: value });
    await screen.findByRole('table', { name: 'Requests' });
    await fireEvent.click(screen.getByRole('radio', { name: 'API clients' }));
    await fireEvent.change(screen.getByRole('combobox', { name: 'Authentication' }), { target: { value: 'api_key' } });
    await fireEvent.click(within(screen.getByRole('radiogroup', { name: 'Status' })).getByRole('radio', { name: 'Errors' }));
    await waitFor(() => expect(requestLog.mock.calls.at(-1)?.[1]).toEqual({ traffic_origins: ['api'], auth_types: ['api_key'], states: ['failed', 'cancelled'] }));
  });

  it('opens client detail with user agent and labels, hidden in project usage', async () => {
    const { value, requestDetail } = source([row()]);
    const { unmount } = render(RequestLogPanel, { query: { ...fixtureQuery, mode: 'personal' }, source: value });
    await fireEvent.click(await screen.findByRole('button', { name: /Request .* details/ }));
    expect(requestDetail).toHaveBeenCalledWith(expect.anything(), 'sel_1', expect.anything());
    expect(screen.getByTitle('my-tool/1.4 (linux)')).toBeTruthy();
    expect(screen.getByText('run-7')).toBeTruthy();
    expect(screen.getByText('/v1/chat/completions')).toBeTruthy();
    unmount();
    render(RequestLogPanel, { query: { ...fixtureQuery, mode: 'project' }, source: source([row()]).value });
    await fireEvent.click(await screen.findByRole('button', { name: /Request .* details/ }));
    expect(screen.queryByText('User agent')).toBeNull();
  });

  it('shows unpriced requests as unpriced, never as zero', async () => {
    const { value } = source([row({ costs: [], unpriced_attempts: 1 })]);
    render(RequestLogPanel, { query: fixtureQuery, source: value });
    expect((await screen.findByRole('table', { name: 'Requests' })).textContent).toContain('Unpriced');
  });

  it('shows native media units and readable operations instead of missing token counts', async () => {
    const quantities: W.MediaQuantity[] = [
      { name: 'output_image_count', unit: 'images', value: 2, source: 'provider', status: 'reported' },
      { name: 'input_audio_duration_ms', unit: 'milliseconds', value: 90000, source: 'adapter', status: 'derived' },
      { name: 'input_characters', unit: 'characters', value: 120, source: 'adapter', status: 'derived' },
      { name: 'output_video_duration_ms', unit: 'milliseconds', value: 8000, source: 'provider', status: 'reported' }
    ];
    const operations = ['image_generate', 'transcribe', 'synthesize', 'video_generate'];
    const { value } = source(quantities.map((quantity, index) => row({
      request_id: `req_media_${index}`, selection_ref: `sel_media_${index}`,
      operations: [operations[index]], media_usage: [quantity],
      tokens: { input: null, output: null, cache_read: null, cache_write: null, reasoning: null }
    })));
    render(RequestLogPanel, { query: fixtureQuery, source: value });
    const table = await screen.findByRole('table', { name: 'Requests' });
    expect(table.textContent).toContain('Image generation');
    expect(table.textContent).toContain('2 images');
    expect(table.textContent).toContain('Speech to text');
    expect(table.textContent).toContain('1.5 audio minutes (derived)');
    expect(table.textContent).toContain('Text to speech');
    expect(table.textContent).toContain('120 characters (derived)');
    expect(table.textContent).toContain('Video generation');
    expect(table.textContent).toContain('8 video seconds');
  });
});
