import { fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import StandaloneViewer from './StandaloneViewer.svelte';
import { deliverableAudio } from './lib/components/ui/private-view-definitions';

// These cases qualify supported controls, not cold Vite module-transform time.
await deliverableAudio.load();

beforeEach(() => vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('{}', { status: 401 }))));
afterEach(() => vi.unstubAllGlobals());
describe('standalone document audio controls', () => {
  it('installs document find without relying on the application layout', async () => {
    const rects = vi.spyOn(HTMLElement.prototype, 'getClientRects').mockReturnValue([
      { width: 100, height: 30, top: 0, left: 0, right: 100, bottom: 30 },
    ] as unknown as DOMRectList);
    try {
      render(StandaloneViewer, { viewerProps: {
        surface: 'standalone', payload: { blocks: [{ type: 'markdown', content: 'Standalone searchable text.' }] },
      } });
      await fireEvent.keyDown(window, { key: 'f', metaKey: true });
      const field = await screen.findByRole('searchbox', { name: 'Find in document' });
      await waitFor(() => expect(field).toHaveFocus());
      await fireEvent.keyDown(field, { key: 'Escape' });
      expect(screen.getByText('Standalone searchable text.')).toBeVisible();
    } finally { rects.mockRestore(); }
  });

  it.each(['markdown', 'plain', 'html', 'rich'])('exposes the same Listen control for %s', async format => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('{}', { status: 401 })));
    const client = { context: vi.fn().mockResolvedValue({
      supported: true, can_generate: true, detail: null, jobs: []
    }), estimates: vi.fn().mockResolvedValue(['shorter', 'default', 'longer'].map(length => ({
      length, target_words: 100, seconds_min: 30, seconds_max: 60
    }))), create: vi.fn(), get: vi.fn(), cancel: vi.fn() };
    const ref = 'dlv_11111111111111111111111111111111';
    render(StandaloneViewer, { viewerProps: {
      surface: 'standalone', instanceId: ref, audioClient: client,
      payload: { blocks: [{ type: 'markdown', content: `A ${format} document.` }] }
    } });
    expect(await screen.findAllByRole('button', { name: 'Listen to this document' })).toHaveLength(1);
    await fireEvent.click(screen.getByRole('button', { name: 'Listen to this document' }));
    await waitFor(() => expect(client.context).toHaveBeenCalledWith(ref, expect.anything()));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Generate narration' })).toBeEnabled());
    await fireEvent.click(screen.getByRole('tab', { name: 'Podcast' }));
    expect(screen.getByRole('button', { name: 'Generate podcast' })).toBeEnabled();
    expect(client.create).not.toHaveBeenCalled();
  });

  it('omits generation controls from offline exports', () => {
    render(StandaloneViewer, { viewerProps: {
      surface: 'standalone', exportMode: true, instanceId: 'dlv_11111111111111111111111111111111',
      payload: { blocks: [{ type: 'markdown', content: 'Offline document.' }] }
    } });
    expect(screen.queryByRole('button', { name: 'Listen to this document' })).not.toBeInTheDocument();
  });

  it('keeps active HTML in an opaque iframe outside the trusted Listen controls', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('{}', { status: 401 })));
    render(StandaloneViewer, { viewerProps: {
      surface: 'standalone', instanceId: 'att_22222222222222222222222222222222',
      embeddedDocumentUrl: '/api/v1/artifacts/view/reports/report/page.html?raw=1',
      payload: { blocks: [] }
    } });
    await waitFor(() => expect(fetch).toHaveBeenCalled());
    expect(screen.queryByRole('button', { name: 'Listen to this document' })).toBeNull();
    const frame = screen.getByTitle('Artifact preview');
    expect(frame).toHaveAttribute('sandbox', 'allow-scripts');
    expect(frame).toHaveAttribute('referrerpolicy', 'no-referrer');
    const find = new KeyboardEvent('keydown', { key: 'f', metaKey: true, cancelable: true });
    window.dispatchEvent(find);
    expect(find.defaultPrevented).toBe(false);
    expect(screen.queryByRole('searchbox', { name: 'Find in document' })).toBeNull();
  });

  it.each([401, 403, 404])('never shows a speaker or sign-in notice without source access (%s)', async status => {
    const fetcher = vi.fn().mockResolvedValue(new Response('{}', { status }));
    vi.stubGlobal('fetch', fetcher);
    render(StandaloneViewer, { viewerProps: {
      surface: 'standalone', instanceId: 'dlv_11111111111111111111111111111111',
      payload: { blocks: [{ type: 'markdown', content: 'Public reading remains available.' }] }
    } });
    await waitFor(() => expect(fetcher.mock.calls.some(([url]) => String(url).includes('/audio/jobs/source?'))).toBe(true));
    expect(screen.getByText('Public reading remains available.')).toBeVisible();
    expect(screen.queryByRole('button', { name: 'Listen to this document' })).toBeNull();
    expect(screen.queryByText(/Sign in with document access/)).toBeNull();
    expect(fetcher.mock.calls.every(([, options]) => !options.method)).toBe(true);
  });

  it.each([false, true])('lets verified account appearance override the publication family (source access=%s)', async sourceAccess => {
    vi.stubGlobal('fetch', vi.fn().mockImplementation(url => Promise.resolve(new Response(
      String(url) === '/api/v1/user-preferences'
        ? JSON.stringify({ display: { theme: 'light', theme_family: 'paper' } }) : '{}',
      { status: String(url) === '/api/v1/user-preferences' ? 200 : 401 }
    ))));
    const client = { context: vi.fn().mockResolvedValue({
      supported: sourceAccess, can_generate: sourceAccess, can_cancel: false, detail: null, jobs: []
    }), estimates: vi.fn(), create: vi.fn(), get: vi.fn(), cancel: vi.fn() };
    const { container } = render(StandaloneViewer, { viewerProps: {
      surface: 'standalone', instanceId: 'dlv_11111111111111111111111111111111', audioClient: client,
      publicationThemeFamily: 'gruvbox',
      payload: { blocks: [{ type: 'markdown', content: 'Public material.' }] }
    } });
    const document = container.querySelector('.rich-deliverable')!;
    await waitFor(() => expect(document).toHaveAttribute('data-theme-family', 'paper'));
    expect(document).toHaveAttribute('data-viewer-theme', 'light');
    await waitFor(() => expect(Boolean(screen.queryByRole('button', { name: 'Listen to this document' }))).toBe(sourceAccess));
    expect(client.create).not.toHaveBeenCalled();
  });

  it.each([false, true])('inherits the publication family without network authority (offline=%s)', async exportMode => {
    const { container } = render(StandaloneViewer, { viewerProps: {
      surface: 'standalone', exportMode, publicationThemeFamily: 'gruvbox',
      payload: { blocks: [{ type: 'markdown', content: 'Shared material.' }] }
    } });
    await waitFor(() => expect(container.querySelector('.rich-deliverable')).toHaveAttribute('data-theme-family', 'gruvbox'));
    if (exportMode) expect(fetch).not.toHaveBeenCalled();
  });

  it('discards late authorization from a previous source revision', async () => {
    let finish!: (value: { supported: boolean; can_generate: boolean; can_cancel: boolean; detail: null; jobs: [] }) => void;
    const client = {
      context: vi.fn().mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }))
        .mockResolvedValue({ supported: false, can_generate: false, can_cancel: false, detail: null, jobs: [] }),
      estimates: vi.fn(), create: vi.fn(), get: vi.fn(), cancel: vi.fn()
    };
    const props = { surface: 'standalone' as const, instanceId: 'dlv_11111111111111111111111111111111', audioClient: client, payload: { blocks: [] } };
    const { rerender } = render(StandaloneViewer, { viewerProps: props });
    await waitFor(() => expect(client.context).toHaveBeenCalledTimes(1));
    expect(screen.queryByRole('button', { name: 'Listen to this document' })).toBeNull();
    await rerender({ viewerProps: { ...props, instanceId: 'dlv_22222222222222222222222222222222' } });
    await waitFor(() => expect(client.context).toHaveBeenCalledTimes(2));
    finish({ supported: true, can_generate: true, can_cancel: false, detail: null, jobs: [] });
    await Promise.resolve();
    expect(screen.queryByRole('button', { name: 'Listen to this document' })).toBeNull();
  });

  it('removes audio controls after session access is lost on refocus', async () => {
    const client = { context: vi.fn().mockResolvedValueOnce({
      supported: true, can_generate: true, can_cancel: false, detail: null, jobs: []
    }).mockResolvedValue({ supported: false, can_generate: false, can_cancel: false, detail: null, jobs: [] }),
    estimates: vi.fn(), create: vi.fn(), get: vi.fn(), cancel: vi.fn() };
    render(StandaloneViewer, { viewerProps: {
      surface: 'standalone', instanceId: 'dlv_11111111111111111111111111111111', audioClient: client,
      payload: { blocks: [] }
    } });
    await screen.findByRole('button', { name: 'Listen to this document' });
    await fireEvent.focus(window);
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Listen to this document' })).toBeNull());
  });
});

describe('standalone selection copy', () => {
  it.each([false, true])('copies Unicode prose as text and HTML (offline=%s)', async (exportMode) => {
    // No owner ID: selection copying must not depend on authenticated sharing.
    const text = 'baterie: kouřový senzor (0 %)';
    const { unmount } = render(StandaloneViewer, {
      viewerProps: {
        surface: 'standalone',
        exportMode,
        content: `${text}\n\nZ-Wave: zkontrolovat napájení.`,
        payload: { blocks: [{ type: 'markdown', content: `${text}\n\nZ-Wave: zkontrolovat napájení.` }] },
      },
    });
    const source = screen.getByText(text);
    const range = document.createRange();
    range.selectNodeContents(source.parentElement!);
    const selection = document.getSelection()!;
    selection.removeAllRanges();
    selection.addRange(range);
    const setData = vi.fn();
    const copy = new Event('copy', { bubbles: true, cancelable: true });
    Object.defineProperty(copy, 'clipboardData', { value: { setData } });

    source.dispatchEvent(copy);

    expect(copy.defaultPrevented).toBe(true);
    expect(setData).toHaveBeenCalledWith('text/plain', `${text}\n\nZ-Wave: zkontrolovat napájení.`);
    expect(setData).toHaveBeenCalledWith('text/html', expect.stringContaining(text));
    expect(setData.mock.calls.map(([type]) => type)).toEqual(['text/html', 'text/plain']);

    await unmount();
    setData.mockClear();
    const afterUnmount = new Event('copy', { bubbles: true, cancelable: true });
    Object.defineProperty(afterUnmount, 'clipboardData', { value: { setData } });
    document.dispatchEvent(afterUnmount);
    expect(setData).not.toHaveBeenCalled();
    selection.removeAllRanges();
  });
});
