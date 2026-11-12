import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { loginFixtureAccount } from '../../../test/verified-account';
import { auth } from '$lib/stores/auth';
import FeedbackHostLoaderHarness from './FeedbackHostLoaderHarness.svelte';

const delayed = vi.hoisted(() => {
  let release!: () => void;
  const ready = new Promise<void>(resolve => { release = resolve; });
  return { ready, release: () => release(), entered: vi.fn(), constructed: vi.fn() };
});
vi.mock('$lib/feedback/state', async (importOriginal) => {
  const original = await importOriginal<typeof import('$lib/feedback/state')>();
  delayed.entered();
  await delayed.ready;
  return { ...original, FeedbackController: class extends original.FeedbackController {
    constructor(...args: ConstructorParameters<typeof original.FeedbackController>) {
      delayed.constructed();
      super(...args);
    }
  } };
});
beforeEach(loginFixtureAccount);
afterEach(async () => { cleanup(); await auth.clear(); });
it('does not initialize a source controller after closing during its deferred module load', async () => {
  render(FeedbackHostLoaderHarness);
  expect(delayed.entered).not.toHaveBeenCalled();
  await fireEvent.click(screen.getByRole('button', { name: /^Comments/ }));
  await waitFor(() => expect(delayed.entered).toHaveBeenCalledOnce());
  await fireEvent.click(await screen.findByRole('button', { name: 'Close comments' }));
  delayed.release();
  await vi.dynamicImportSettled();
  expect(delayed.constructed).not.toHaveBeenCalled();
  expect(screen.queryByRole('dialog', { name: 'Feedback' })).toBeNull();
});
