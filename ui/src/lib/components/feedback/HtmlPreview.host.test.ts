import 'fake-indexeddb/auto';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';
import { flushSync } from 'svelte';
import { loginFixtureAccount } from '../../../test/verified-account';
import { auth } from '$lib/stores/auth';
import { FeedbackController } from '$lib/feedback/state';
import { documentCoordination } from '$lib/document-coordination';
import type { ResolvedFeedbackSource } from '$lib/feedback/types';
import type { mountHtmlPreview } from '$lib/attachments/html-preview';
import Harness from './HtmlFeedbackHostHarness.svelte';

const mocks = vi.hoisted(() => ({
  resolve: vi.fn(), threads: vi.fn(), draft: vi.fn(), saveDraft: vi.fn(),
  mounts: [] as { annotation: NonNullable<Parameters<typeof mountHtmlPreview>[4]>; dispose: ReturnType<typeof vi.fn> }[],
  digest: vi.fn(), crop: vi.fn(), dimensions: vi.fn(),
}));
vi.mock('$lib/feedback/api', () => ({ feedbackApi: {
  resolve: mocks.resolve, threads: mocks.threads, draft: mocks.draft, saveDraft: mocks.saveDraft,
} }));
vi.mock('$lib/attachments/html-preview', () => ({
  htmlPreviewGuard: (source: { guard: unknown }) => source.guard,
  mountHtmlPreview: (...args: Parameters<typeof mountHtmlPreview>) => {
    const annotation = args[4]!;
    const dispose = vi.fn(() => annotation.ready?.(null));
    mocks.mounts.push({ annotation, dispose });
    args[3](''); annotation.ready?.(async () => true);
    return dispose;
  },
}));
vi.mock('$lib/feedback/annotations/digest', () => ({
  textDigest: mocks.digest, bytesDigest: vi.fn().mockResolvedValue('frozen-pixel-digest'),
}));
vi.mock('$lib/feedback/annotations/capture', () => ({
  cropCapture: mocks.crop, rasterDimensions: mocks.dimensions, captureScreen: vi.fn(),
}));

const source: ResolvedFeedbackSource = {
  subject_id: 'html-host-subject', source_token: 'html-host-source-proof',
  source: { kind: 'document', content_ref: 'art_4ad53de97cfe4253931563af979f4d0b' },
  context: { kind: 'chat', conversation_id: 'html-host-origin' },
  content_digest: 'a'.repeat(64), lineage_id: null, revision: 1, latest_content_ref: null,
  title: 'HTML host regression', format: 'html', availability: 'available',
  capabilities: { read: true, comment: true, dispatch: false, moderate: false, disclose: false },
  canonical_text: null, permitted_locator_kinds: ['whole', 'html'],
};
const viewport = { width_css_px: 640, height_css_px: 400, scroll_x_css_px: 0, scroll_y_css_px: 0, device_pixel_ratio: 1 };
const observed = { selector: 'html > body > h1', element_key: null, quote: '', viewport,
  selection_kind: 'element' as const, start: 0, end: 0, rects: [{ left: 20, top: 30, width: 100, height: 30 }], scopes: [] };
const originalURL = globalThis.URL;
function bindingSpy() { return vi.spyOn(FeedbackController.prototype, 'binding'); }
let spy: ReturnType<typeof bindingSpy>;
beforeEach(async () => {
  await loginFixtureAccount();
  mocks.mounts.length = 0;
  mocks.resolve.mockResolvedValue(structuredClone(source));
  mocks.threads.mockResolvedValue({ items: [], next_cursor: null });
  mocks.draft.mockRejectedValue(Object.assign(new Error('Draft not found'), { status: 404 }));
  mocks.saveDraft.mockResolvedValue({ version: 1 });
  mocks.digest.mockReset().mockResolvedValue('observed-render-digest');
  mocks.dimensions.mockResolvedValue({ width: 640, height: 400 });
  const raster = new Blob(['raster'], { type: 'image/png' });
  Object.defineProperty(raster, 'arrayBuffer', { value: async () => new ArrayBuffer(4) });
  mocks.crop.mockReset().mockResolvedValue(raster);
  globalThis.URL = class extends originalURL {
    static createObjectURL = vi.fn(() => 'blob:https://host.test/capture');
    static revokeObjectURL = vi.fn();
  };
  spy = bindingSpy();
});
afterEach(async () => {
  cleanup(); spy.mockRestore(); globalThis.URL = originalURL; await auth.clear();
});
async function setup(mode: 'comment' | 'area' = 'comment', accountGuard = false) {
  const abort = new AbortController();
  const guard = { signal: abort.signal, assertCurrent: vi.fn(), validate: vi.fn() };
  const props = { source: source.source, context: source.context,
    preview: { text: '<h1>Actual host</h1>', guard: accountGuard ? documentCoordination().capture() : guard } };
  const view = render(Harness, { props });
  await fireEvent.click(screen.getByRole('button', { name: /^Comments/ }));
  await waitFor(() => expect(spy.mock.results.some(result => result.value)).toBe(true));
  const controller = spy.mock.contexts.at(-1)!;
  if (!(controller instanceof FeedbackController)) throw new Error('Host did not bind a real controller.');
  await controller.discardDraft();
  flushSync(() => controller.patch({ mode, open: false }));
  return { ...view, controller, abort, guard, props };
}
function observe() {
  flushSync(() => mocks.mounts.at(-1)!.annotation.observe(observed));
}
const comment = () => screen.getByRole('button', { name: 'Add comment' });

describe('HTML observations through real FeedbackHost/controller snapshot churn', () => {
  it('retains an observation across interaction, selection and draft snapshots and emits through the current binding', async () => {
    const { controller } = await setup();
    observe();
    expect(get(controller.state).interacting).toBe(true);
    await screen.findByRole('button', { name: 'Add comment' });
    const before = spy.mock.calls.length;
    flushSync(() => controller.patch({ selected: 'another-thread', interacting: true }));
    expect(spy.mock.calls.length).toBeGreaterThan(before);
    expect(comment()).toBeEnabled();
    await fireEvent.click(comment());
    await waitFor(() => expect(get(controller.state).candidate?.locator).toMatchObject({
      kind: 'html', selector_path: 'html > body > h1', viewport, render_state_digest: 'observed-render-digest',
    }));
    expect(mocks.mounts).toHaveLength(1);
  });
  it.each([
    ['token', { source_token: 'replacement-source-proof' }],
    ['digest', { content_digest: 'b'.repeat(64) }],
    ['subject', { subject_id: 'replacement-subject' }],
    ['authority', { capabilities: { ...source.capabilities, comment: false } }],
    ['availability', { availability: 'unavailable' as const }],
    ['locator authority', { permitted_locator_kinds: ['whole'] as ResolvedFeedbackSource['permitted_locator_kinds'] }],
  ])('invalidates on meaningful %s replacement, including pending digest work', async (_name, replacement) => {
    const { controller } = await setup();
    let release!: (value: string) => void;
    mocks.digest.mockImplementationOnce(() => new Promise<string>(resolve => { release = resolve; }));
    observe();
    flushSync(() => controller.patch({ source: { ...source, ...replacement } }));
    release('late-digest');
    await waitFor(() => expect(get(controller.state).interacting).toBe(false));
    expect(get(controller.state).candidate).toBeNull();
    expect(screen.queryByRole('button', { name: 'Add comment' })).toBeNull();
  });
  it('rejects a late guard validation result without publishing its error into a replacement scope', async () => {
    const { controller, guard } = await setup();
    let reject!: (cause: Error) => void;
    guard.validate.mockImplementationOnce(() => new Promise<void>((_resolve, fail) => { reject = fail; }));
    observe();
    await fireEvent.click(await screen.findByRole('button', { name: 'Add comment' }));
    await waitFor(() => expect(guard.validate).toHaveBeenCalled());
    flushSync(() => controller.patch({ source: { ...source, source_token: 'replacement-source-proof' } }));
    reject(new Error('Expired old validation'));
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Add comment' })).toBeNull());
    expect(screen.queryByText('Expired old validation')).toBeNull();
    expect(get(controller.state).candidate).toBeNull();
  });
  it('clears on mode changes and render replacement and rejects callbacks from disposed mounts', async () => {
    const view = await setup(); observe();
    const old = mocks.mounts[0];
    flushSync(() => view.controller.patch({ mode: 'view' }));
    flushSync(() => view.controller.patch({ mode: 'comment' }));
    expect(screen.queryByRole('button', { name: 'Add comment' })).toBeNull();
    observe(); await screen.findByRole('button', { name: 'Add comment' });
    await view.rerender({ ...view.props, preview: { ...view.props.preview, text: '<h1>New render</h1>' } });
    expect(old.dispose).toHaveBeenCalled();
    flushSync(() => old.annotation.observe(observed));
    expect(screen.queryByRole('button', { name: 'Add comment' })).toBeNull();
  });
  it('clears on context replacement and guard revocation and cannot emit after unmount', async () => {
    const view = await setup(); observe();
    await view.rerender({ ...view.props, context: { kind: 'chat', conversation_id: 'replacement-context' } });
    expect(get(view.controller.state).candidate).toBeNull();
    expect(screen.queryByRole('button', { name: 'Add comment' })).toBeNull();
    view.unmount();
    const second = await setup(); observe();
    second.abort.abort();
    flushSync(() => mocks.mounts.at(-1)!.annotation.ready?.(null));
    expect(screen.queryByRole('button', { name: 'Add comment' })).toBeNull();
    const mount = mocks.mounts.at(-1)!;
    second.unmount();
    flushSync(() => mount.annotation.observe(observed));
    expect(get(second.controller.state).candidate).toBeNull();
  });
  it('keeps acquisition and confirmed pixels through real interaction/selection churn without changing ObservedCapture', async () => {
    const { controller } = await setup('area');
    await fireEvent.change(screen.getByLabelText(/Choose.*screenshot/), { target: {
      files: [new File(['png'], 'screenshot.png', { type: 'image/png' })],
    } });
    await screen.findByAltText('Unconfirmed screenshot, local only');
    flushSync(() => controller.patch({ selected: 'another-thread', interacting: true }));
    await fireEvent.click(screen.getByLabelText(/The crop shows the entire intended artifact viewport/));
    await fireEvent.click(screen.getByText('Confirm crop and freeze'));
    await screen.findByAltText('Confirmed frozen HTML observation');
    flushSync(() => controller.patch({ selected: null, interacting: true }));
    expect(screen.getByAltText('Confirmed frozen HTML observation')).toBeTruthy();
    await fireEvent.click(screen.getByText('Add pin'));
    await fireEvent.click(await screen.findByRole('button', { name: 'Add comment' }));
    expect(get(controller.state).candidate?.capture).toMatchObject({
      blob: expect.any(Blob), width: 640, height: 400, method: 'observed_pixels',
    });
    flushSync(() => controller.patch({ source: { ...source, content_digest: 'b'.repeat(64) } }));
    expect(screen.queryByAltText('Confirmed frozen HTML observation')).toBeNull();
  });
  it.each(['comment', 'area'] as const)('drops %s state on actual account lease revocation without calling an expired controller', async mode => {
    const view = await setup(mode, true);
    if (mode === 'comment') observe();
    else {
      await fireEvent.change(screen.getByLabelText(/Choose.*screenshot/), { target: {
        files: [new File(['png'], 'screenshot.png', { type: 'image/png' })],
      } });
      await screen.findByAltText('Unconfirmed screenshot, local only');
    }
    await documentCoordination().beginLogin();
    expect(() => flushSync(() => mocks.mounts.at(-1)!.annotation.ready?.(null))).not.toThrow();
    expect(get(view.controller.state).candidate).toBeNull();
    expect(screen.queryByAltText('Unconfirmed screenshot, local only')).toBeNull();
    view.unmount();
  });
});
