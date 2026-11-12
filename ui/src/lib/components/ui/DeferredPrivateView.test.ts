import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { loginFixtureAccount } from '../../../test/verified-account';
import { auth } from '$lib/stores/auth';
import { deferredDefinition } from './deferred-definition';
import Harness from './DeferredPrivateViewHarness.svelte';
import Content from './DeferredPrivateViewContent.svelte';

beforeEach(loginFixtureAccount);
afterEach(async () => { cleanup(); await auth.clear(); });

function delayed() {
  let resolve!: (value: { default: typeof Content }) => void;
  const promise = new Promise<{ default: typeof Content }>(done => { resolve = done; });
  const load = vi.fn(() => promise);
  return { definition: deferredDefinition(load), load, release: () => resolve({ default: Content }) };
}
it('drops a cold completion after the view closes', async () => {
  const module = delayed();
  const mounted = render(Harness, { definition: module.definition });
  await waitFor(() => expect(module.load).toHaveBeenCalledOnce());
  await mounted.rerender({ definition: module.definition, visible: false });
  module.release();
  await waitFor(() => expect(module.definition.peek()).toBe(Content));
  expect(screen.queryByLabelText('Loaded source')).toBeNull();
});
it('uses only the current source after a cold source switch', async () => {
  const module = delayed();
  const mounted = render(Harness, { definition: module.definition });
  await mounted.rerender({ definition: module.definition, source: 'second' });
  module.release();
  expect(await screen.findByLabelText('Loaded source')).toHaveValue('second');
  expect(module.load).toHaveBeenCalledOnce();
});
it('does not mount an old account view after invalidation', async () => {
  const module = delayed();
  render(Harness, { definition: module.definition });
  await waitFor(() => expect(module.load).toHaveBeenCalledOnce());
  await auth.clear();
  module.release();
  await waitFor(() => expect(module.definition.peek()).toBe(Content));
  expect(screen.queryByLabelText('Loaded source')).toBeNull();
});
it('retains warm DOM and focus and reopens synchronously using only the cached definition', async () => {
  const module = delayed();
  const mounted = render(Harness, { definition: module.definition });
  module.release();
  const input = await screen.findByLabelText('Loaded source');
  input.focus();
  await mounted.rerender({ definition: module.definition, source: 'second' });
  expect(screen.getByLabelText('Loaded source')).toBe(input);
  expect(document.activeElement).toBe(input);
  await mounted.rerender({ definition: module.definition, visible: false });
  await mounted.rerender({ definition: module.definition, visible: true, source: 'third' });
  expect(screen.getByLabelText('Loaded source')).toHaveValue('third');
  expect(screen.queryByRole('status')).toBeNull();
  expect(module.load).toHaveBeenCalledOnce();
});
it('can retry a rejected definition without retaining the rejection', async () => {
  const load = vi.fn()
    .mockRejectedValueOnce(new Error('The view download failed.'))
    .mockResolvedValueOnce({ default: Content });
  render(Harness, { definition: deferredDefinition<{ name: string }>(load) });
  expect(await screen.findByRole('alert')).toHaveTextContent('The view download failed.');
  await fireEvent.click(screen.getByRole('button', { name: 'Retry test view' }));
  expect(await screen.findByLabelText('Loaded source')).toHaveValue('first');
  expect(load).toHaveBeenCalledTimes(2);
});
it('keeps pending modal keyboard input inside the view and restores opener focus on cancellation', async () => {
  const module = delayed();
  const opener = document.createElement('button');
  document.body.append(opener); opener.focus();
  const close = vi.fn();
  const mounted = render(Harness, { definition: module.definition, modal: true, onClose: close });
  await screen.findByRole('dialog', { name: 'test view' });
  await fireEvent.keyDown(window, { key: 'Tab' });
  expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Close test view' }));
  await fireEvent.keyDown(window, { key: 'Tab', shiftKey: true });
  expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Close test view' }));
  await fireEvent.keyDown(window, { key: 'Escape' });
  expect(close).toHaveBeenCalledOnce();
  mounted.unmount();
  expect(document.activeElement).toBe(opener);
  opener.remove(); module.release();
});
it('restores the original opener after a cold modal loads successfully and then closes', async () => {
  const module = delayed();
  const opener = document.createElement('button');
  document.body.append(opener); opener.focus();
  const mounted = render(Harness, { definition: module.definition, modal: true });
  await screen.findByRole('dialog', { name: 'test view' });
  module.release();
  (await screen.findByLabelText('Loaded source')).focus();
  mounted.unmount();
  expect(document.activeElement).toBe(opener);
  opener.remove();
});
