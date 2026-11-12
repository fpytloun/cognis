import { afterEach, expect, it, vi } from 'vitest';
import { waitFor } from '@testing-library/svelte';
import { chatTextFeedback } from './chat-text-feedback';
import type { FeedbackSurfaceBinding } from './types';

const delayed = vi.hoisted(() => {
  let release!: () => void;
  const ready = new Promise<void>(resolve => { release = resolve; });
  return { ready, release: () => release(), entered: vi.fn(), mount: vi.fn(), account: 0 };
});
vi.mock('./chat-text-adapter', async () => {
  delayed.entered();
  await delayed.ready;
  return { mountChatTextAdapter: delayed.mount };
});
vi.mock('$lib/document-coordination', () => ({
  documentCoordination: () => ({ capture: () => {
    const account = delayed.account;
    return { assertCurrent: () => { if (account !== delayed.account) throw new Error('Account replaced'); } };
  } }),
}));
afterEach(() => { delayed.release(); document.body.replaceChildren(); });

it('does not mount old bindings under a new account after the second lazy import', async () => {
  const root = document.createElement('div'); root.textContent = 'Saved target'; document.body.append(root);
  const registerAdapter = vi.fn(), onTargetChange = vi.fn(), error = vi.fn();
  const binding = {
    source: { source_token: crypto.randomUUID(), content_digest: crypto.randomUUID(),
      capabilities: { read: true, comment: true }, availability: 'available' },
    context: { kind: 'standalone' }, mode: 'comment', registerAdapter, onTargetChange,
  } as unknown as FeedbackSurfaceBinding;
  const action = chatTextFeedback(root, {
    binding, key: crypto.randomUUID(), enabled: true, text: root.textContent, error,
    surface: { binding, available: true, open: vi.fn(), commenting: vi.fn() },
  });
  await waitFor(() => expect(delayed.entered).toHaveBeenCalledOnce());
  delayed.account++;
  delayed.release();
  await vi.dynamicImportSettled();
  expect(delayed.mount).not.toHaveBeenCalled();
  expect(registerAdapter).not.toHaveBeenCalled();
  expect(onTargetChange).not.toHaveBeenCalled();
  expect(error).toHaveBeenCalledWith(expect.stringContaining('could not load'));
  action.destroy();
});
