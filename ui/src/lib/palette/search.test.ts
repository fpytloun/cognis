import { afterEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';
import { PaletteSearch, type FetchPage } from './search';
import type { PaletteItem, PalettePage } from './model';
const row = (id: string): PaletteItem => ({ kind: 'conversation', id, title: id, href: `/chat/${id}`,
  project_id: null, agent_id: null, status: 'active', format: null, updated_at: '' });
const page = (ids: string[], cursor: string | null = null): PalettePage => ({ items: ids.map(row), next_cursor: cursor });
const deferred = () => {
  let resolve!: (value: PalettePage) => void;
  const promise = new Promise<PalettePage>((done) => { resolve = done; });
  return { promise, resolve };
};
afterEach(() => vi.useRealTimers());
describe('bounded palette transport/cache', () => {
  it('keeps loading through busy admission and recovers without manual retry', async () => {
    vi.useFakeTimers();
    const busy = Object.assign(new Error('Search is busy'), { code: 'search_busy' });
    const fetch = vi.fn().mockRejectedValueOnce(busy).mockRejectedValueOnce(busy).mockResolvedValue(page(['ready']));
    const search = new PaletteSearch(fetch);
    search.setQuery('cold'); await vi.advanceTimersByTimeAsync(250);
    expect(get(search)).toMatchObject({ loading: true, error: '' });
    await vi.advanceTimersByTimeAsync(2000);
    expect(get(search).items.map(item => item.id)).toEqual(['ready']);
    expect(get(search).loading).toBe(false);
    search.clear();
  });
  it('bounds admission retries and exposes an explicit retry after sustained contention', async () => {
    vi.useFakeTimers();
    const fetch = vi.fn().mockRejectedValue(Object.assign(new Error('Search is busy'), { code: 'search_busy' }));
    const search = new PaletteSearch(fetch);
    search.setQuery('busy'); await vi.advanceTimersByTimeAsync(31_000);
    expect(get(search)).toMatchObject({ loading: false, error: 'Search is busy' });
    const calls = fetch.mock.calls.length;
    await vi.advanceTimersByTimeAsync(60_000);
    expect(fetch).toHaveBeenCalledTimes(calls);
    search.clear();
  });
  it('retires a busy retry on query change and close without publishing old results', async () => {
    vi.useFakeTimers();
    const busy = Object.assign(new Error('Search is busy'), { code: 'search_busy' });
    const fetch = vi.fn().mockRejectedValueOnce(busy).mockResolvedValueOnce(page(['new'])).mockRejectedValue(busy);
    const search = new PaletteSearch(fetch);
    search.setQuery('old'); await vi.advanceTimersByTimeAsync(250);
    search.setQuery('new'); await vi.advanceTimersByTimeAsync(1000);
    expect(get(search).items.map(item => item.id)).toEqual(['new']);
    expect(fetch).toHaveBeenCalledTimes(2);
    search.setQuery('closing'); await vi.advanceTimersByTimeAsync(250);
    search.close(); await vi.advanceTimersByTimeAsync(5000);
    expect(fetch).toHaveBeenCalledTimes(3);
    search.clear();
  });
  it('retries the busy continuation without losing the existing page', async () => {
    vi.useFakeTimers();
    const fetch = vi.fn().mockResolvedValueOnce(page(['first'], 'next'))
      .mockRejectedValueOnce(Object.assign(new Error('Busy'), { code: 'search_busy' }))
      .mockResolvedValueOnce(page(['second']));
    const search = new PaletteSearch(fetch);
    search.setQuery('report'); await vi.advanceTimersByTimeAsync(250);
    search.more(); await vi.advanceTimersByTimeAsync(100);
    expect(get(search)).toMatchObject({ loading: true, error: '' });
    expect(get(search).items.map(item => item.id)).toEqual(['first']);
    await vi.advanceTimersByTimeAsync(1000);
    expect(fetch.mock.calls[2]?.[1]).toBe('next');
    expect(get(search).items.map(item => item.id)).toEqual(['first', 'second']);
    search.clear();
  });
  it('scopes pages, cursors and cache entries to selected types', async () => {
    vi.useFakeTimers();
    const task: PaletteItem = { ...row('report-task'), kind: 'task' };
    const fetch = vi.fn<FetchPage>(async (_query, _cursor, _signal, types) => ({
      items: types?.includes('task') ? [task] : [row('report-conversation')], next_cursor: null,
    }));
    const search = new PaletteSearch(fetch);
    search.setQuery('report', ['task']); await vi.advanceTimersByTimeAsync(250);
    expect(get(search).items.map(item => item.kind)).toEqual(['task']);
    search.setQuery('report', ['conversation']);
    expect(get(search).items).toEqual([]);
    await vi.advanceTimersByTimeAsync(250);
    expect(get(search).items.map(item => item.kind)).toEqual(['conversation']);
    search.setQuery('report', ['task']);
    expect(get(search).items.map(item => item.kind)).toEqual(['task']);
    search.setQuery('report', []);
    expect(get(search).items).toEqual([]);
    expect(get(search).loading).toBe(false);
    await vi.advanceTimersByTimeAsync(500);
    expect(fetch).toHaveBeenCalledTimes(2);
    search.clear();
  });
  it('coalesces rapid typing and reuses fresh first pages', async () => {
    vi.useFakeTimers();
    const fetch = vi.fn<FetchPage>(async () => page(['command-palette']));
    const search = new PaletteSearch(fetch);
    search.setQuery('c'); search.setQuery('co'); search.setQuery('command');
    await vi.advanceTimersByTimeAsync(250);
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(fetch.mock.calls[0]?.[0]).toBe('command');
    search.setQuery('command');
    expect(get(search).items[0]?.id).toBe('command-palette');
    await vi.advanceTimersByTimeAsync(300);
    expect(fetch).toHaveBeenCalledTimes(1);
    search.clear();
  });
  it('never admits a second transport while a stale flight has not settled', async () => {
    vi.useFakeTimers();
    const first = deferred();
    const fetch = vi.fn().mockReturnValueOnce(first.promise).mockResolvedValue(page(['new']));
    const search = new PaletteSearch(fetch);
    search.setQuery('old'); await vi.advanceTimersByTimeAsync(250);
    search.setQuery('new'); await vi.advanceTimersByTimeAsync(1000);
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(fetch.mock.calls[0]?.[2].aborted).toBe(false);
    first.resolve(page(['old']));
    await vi.advanceTimersByTimeAsync(0);
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(get(search).items.map((item) => item.id)).toEqual(['new']);
    search.clear();
  });
  it('deduplicates infinite-scroll pages and closes without a late publish', async () => {
    vi.useFakeTimers();
    const late = deferred();
    const fetch = vi.fn().mockResolvedValueOnce(page(['one', 'two'], 'next-page')).mockReturnValueOnce(late.promise);
    const search = new PaletteSearch(fetch);
    search.setQuery(''); await vi.advanceTimersByTimeAsync(250);
    search.more(true); search.more(true);
    await vi.advanceTimersByTimeAsync(250);
    expect(fetch).toHaveBeenCalledTimes(2);
    late.resolve(page(['two', 'three']));
    await vi.advanceTimersByTimeAsync(0);
    expect(get(search).items.map((item) => item.id)).toEqual(['one', 'two', 'three']);
    search.clear();
    expect(get(search).items).toEqual([]);
  });
  it('expires cached metadata and never retries a failure automatically', async () => {
    vi.useFakeTimers();
    const fetch = vi.fn().mockResolvedValueOnce(page(['report'])).mockRejectedValue(new Error('Busy'));
    const search = new PaletteSearch(fetch);
    search.setQuery('report'); await vi.advanceTimersByTimeAsync(250);
    search.close();
    await vi.advanceTimersByTimeAsync(16_000);
    search.setQuery('report');
    expect(get(search).items).toEqual([]);
    await vi.advanceTimersByTimeAsync(1000);
    expect(get(search).error).toBe('Busy');
    expect(fetch).toHaveBeenCalledTimes(2);
    search.clear();
  });
  it('pauses consecutive sparse continuations until explicitly requested', async () => {
    vi.useFakeTimers();
    let count = 0;
    const fetch = vi.fn(async () => page([], `cursor-${++count}`));
    const search = new PaletteSearch(fetch);
    search.setQuery('broad'); await vi.advanceTimersByTimeAsync(250);
    search.more(); await vi.advanceTimersByTimeAsync(250);
    expect(get(search).paused).toBe(true);
    search.more(); await vi.advanceTimersByTimeAsync(2000);
    expect(fetch).toHaveBeenCalledTimes(2);
    search.more(true); await vi.advanceTimersByTimeAsync(250);
    expect(fetch).toHaveBeenCalledTimes(3);
    search.clear();
  });
  it('does not retry an errored cursor from navigation or scrolling', async () => {
    vi.useFakeTimers();
    const fetch = vi.fn().mockResolvedValueOnce(page(['first'], 'second-page'))
      .mockRejectedValueOnce(new Error('Busy')).mockResolvedValue(page(['second']));
    const search = new PaletteSearch(fetch);
    search.setQuery(''); await vi.advanceTimersByTimeAsync(250);
    search.more(true); await vi.advanceTimersByTimeAsync(250);
    expect(get(search).error).toBe('Busy');
    for (let i = 0; i < 10; i++) search.more();
    await vi.advanceTimersByTimeAsync(2000);
    expect(fetch).toHaveBeenCalledTimes(2);
    search.retry(); await vi.advanceTimersByTimeAsync(250);
    expect(fetch).toHaveBeenCalledTimes(3);
    expect(fetch.mock.calls[2]?.[1]).toBe('second-page');
    expect(get(search).items.map((item) => item.id)).toEqual(['first', 'second']);
    search.clear();
  });
  it('invalidates routine caches without disrupting selection or generating background reads', async () => {
    vi.useFakeTimers();
    const late = deferred();
    const fetch = vi.fn().mockResolvedValueOnce(page(['report'])).mockReturnValueOnce(late.promise).mockResolvedValue(page(['renamed']));
    const search = new PaletteSearch(fetch);
    search.setQuery('report'); await vi.advanceTimersByTimeAsync(250);
    search.invalidate();
    expect(get(search).items[0]?.id).toBe('report');
    expect(get(search).error).toBe('');
    await vi.advanceTimersByTimeAsync(1000);
    expect(fetch).toHaveBeenCalledTimes(1);
    search.setQuery('report'); await vi.advanceTimersByTimeAsync(250);
    search.invalidate();
    late.resolve(page(['stale-report']));
    await vi.advanceTimersByTimeAsync(0);
    search.setQuery('report'); await vi.advanceTimersByTimeAsync(250);
    expect(fetch).toHaveBeenCalledTimes(3);
    expect(get(search).items[0]?.id).toBe('renamed');
    search.clear();
  });
  it('retires visible metadata and fences late responses on access invalidation', async () => {
    vi.useFakeTimers();
    const late = deferred();
    const fetch = vi.fn().mockResolvedValueOnce(page(['private-report'], 'continuation'))
      .mockReturnValueOnce(late.promise).mockResolvedValue(page(['reauthorized-report']));
    const search = new PaletteSearch(fetch);
    search.setQuery(''); await vi.advanceTimersByTimeAsync(250);
    search.more(true); await vi.advanceTimersByTimeAsync(250);
    search.invalidate(true);
    expect(get(search).items).toEqual([]);
    expect(get(search).error).toBe('');
    expect(get(search).loading).toBe(true);
    await vi.advanceTimersByTimeAsync(500);
    expect(fetch).toHaveBeenCalledTimes(2);
    late.resolve(page(['private-late-report']));
    await vi.advanceTimersByTimeAsync(1000);
    expect(get(search).items.map(item => item.id)).toEqual(['reauthorized-report']);
    expect(fetch).toHaveBeenCalledTimes(3);
    expect(fetch.mock.calls[2]?.[1]).toBeNull();
    search.clear();
  });
  it('does not reopen a closed palette on access invalidation', async () => {
    vi.useFakeTimers();
    const fetch = vi.fn().mockResolvedValue(page(['report']));
    const search = new PaletteSearch(fetch);
    search.setQuery(''); await vi.advanceTimersByTimeAsync(250);
    search.close(); search.invalidate(true);
    await vi.advanceTimersByTimeAsync(1000);
    expect(get(search).items).toEqual([]);
    expect(get(search).loading).toBe(false);
    expect(fetch).toHaveBeenCalledTimes(1);
    search.clear();
  });
  it('keeps an idle blank-query snapshot stable past cache expiry and observer notifications', async () => {
    vi.useFakeTimers();
    const fetch = vi.fn().mockResolvedValueOnce(page(['recent-task'], 'next-page'))
      .mockResolvedValue(page(['older-file']));
    const search = new PaletteSearch(fetch);
    search.setQuery(''); await vi.advanceTimersByTimeAsync(250);
    for (let i = 0; i < 10; i++) { search.invalidate(); search.more(); }
    await vi.advanceTimersByTimeAsync(60_000);
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(get(search).items.map(item => item.id)).toEqual(['recent-task']);
    search.more(true); await vi.advanceTimersByTimeAsync(250);
    expect(get(search).items.map(item => item.id)).toEqual(['recent-task', 'older-file']);
    search.clear();
  });
  it('never paints an unranked cross-query cache before the real ranked response', async () => {
    vi.useFakeTimers();
    const late = deferred();
    const fetch = vi.fn().mockResolvedValueOnce(page(['coffee-file'])).mockReturnValueOnce(late.promise);
    const search = new PaletteSearch(fetch);
    search.setQuery('coffee'); await vi.advanceTimersByTimeAsync(250);
    search.setQuery('coff');
    expect(get(search).items).toEqual([]);
    await vi.advanceTimersByTimeAsync(250);
    late.resolve(page(['coffee-task', 'coffee-file'])); await vi.advanceTimersByTimeAsync(0);
    expect(get(search).items.map(item => item.id)).toEqual(['coffee-task', 'coffee-file']);
    search.clear();
  });
});
