import { cleanup, render, screen } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { DocumentLease } from '$lib/document-coordination';
import HtmlPreview from './HtmlPreview.svelte';
import ResourceReader from './library/ResourceReader.svelte';

const mocks = vi.hoisted(() => ({ capture: vi.fn(), signedUrl: vi.fn(), revisions: vi.fn() }));
vi.mock('$lib/config', () => ({ apiUrl: (path: string) => `https://cognis.test${path}` }));
vi.mock('$app/navigation', () => ({ beforeNavigate: vi.fn() }));
vi.mock('$lib/document-coordination', () => ({
  documentCoordination: () => ({
    capture: mocks.capture,
    onInvalidated: () => () => {},
  }),
}));
vi.mock('$lib/api/client', () => ({ api: {
  artifacts: { signedUrl: mocks.signedUrl, revisions: mocks.revisions },
  library: { item: vi.fn().mockRejectedValue(new Error('Not needed')) },
} }));
vi.mock('$lib/stores/auth', async () => {
  const { writable } = await import('svelte/store');
  return { auth: writable({ user: { email: 'reader@test.invalid', role: 'viewer' } }) };
});
function ownership() {
  const controller = new AbortController();
  const lease: DocumentLease = {
    owner: 'reader', epoch: '1', signal: controller.signal,
    assertCurrent() { controller.signal.throwIfAborted(); },
    async validate() { controller.signal.throwIfAborted(); },
  };
  return { controller, lease };
}
describe('HTML preview surfaces and replacement', () => {
  beforeEach(() => {
    URL.createObjectURL = vi.fn(() => `blob:https://cognis.test/${crypto.randomUUID()}`);
    URL.revokeObjectURL = vi.fn();
    mocks.capture.mockReturnValue(ownership().lease);
    mocks.signedUrl.mockResolvedValue({
      url: `https://cognis.test/api/v1/artifacts/view/attachments/att_html/page.html?exp=9999999999&sig=${'a'.repeat(64)}`,
    });
    mocks.revisions.mockResolvedValue({ revisions: [] });
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async () =>
      new Response('<h1>Preview</h1>', { headers: { 'content-type': 'text/html' } })));
    vi.stubGlobal('matchMedia', (media: string) => Object.assign(new EventTarget(), { matches: false, media }));
  });
  afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
  it('removes the old context before replacement and releases both on close', async () => {
    const { lease } = ownership();
    const view = render(HtmlPreview, { source: { text: '<h1>A</h1>', lease } });
    const old = await screen.findByTitle('Artifact preview');
    await view.rerender({ source: { text: '<h1>B</h1>', lease } });
    await vi.waitFor(() => expect(URL.createObjectURL).toHaveBeenCalledTimes(2));
    expect(old.isConnected).toBe(false);
    expect(URL.revokeObjectURL).toHaveBeenCalledTimes(1);
    view.unmount();
    expect(URL.revokeObjectURL).toHaveBeenCalledTimes(2);
  });
  it('does not resurrect inline content on a prop update after logout', async () => {
    const { lease, controller } = ownership();
    const view = render(HtmlPreview, { source: { text: '<h1>A</h1>', lease } });
    await screen.findByTitle('Artifact preview');
    controller.abort();
    expect(view.container.querySelector('iframe')).toBeNull();
    await view.rerender({ source: { text: '<h1>B</h1>', lease } });
    expect(await screen.findByRole('alert')).toBeVisible();
    expect(URL.createObjectURL).toHaveBeenCalledTimes(1);
  });
  it('renders the actual full/library path using the original signed-access lease', async () => {
    render(ResourceReader, { item: {
      kind: 'artifact', id: 'att_html', title: 'page.html', format: 'text/html',
      is_readonly: true, size_bytes: 20,
    } as never });
    const frame = await screen.findByTitle('Artifact preview');
    expect(frame).toHaveAttribute('sandbox', 'allow-scripts');
    expect(frame.getAttribute('src')).toMatch(/^blob:/);
    expect(mocks.signedUrl).toHaveBeenCalledWith('att_html', 3600, 'view', expect.any(AbortSignal));
  });
  it('does not rebind a resolved library URL after account switch during revision loading', async () => {
    const { lease, controller } = ownership();
    mocks.capture.mockReturnValue(lease);
    let finish!: (value: { revisions: never[] }) => void;
    mocks.revisions.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    render(ResourceReader, { item: {
      kind: 'artifact', id: 'att_html', title: 'page.html', format: 'text/html', is_readonly: true,
    } as never });
    await vi.waitFor(() => expect(mocks.signedUrl).toHaveBeenCalled());
    await new Promise(resolve => setTimeout(resolve, 0));
    controller.abort();
    mocks.capture.mockReturnValue(ownership().lease);
    finish({ revisions: [] });
    await screen.findByRole('alert');
    expect(vi.mocked(fetch).mock.calls.map(([url]) => String(url))).not.toEqual(
      expect.arrayContaining([expect.stringContaining('/artifacts/view/attachments/att_html/')]),
    );
    expect(screen.queryByTitle('Artifact preview')).toBeNull();
  });
});
