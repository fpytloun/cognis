import { afterEach, describe, expect, it, vi } from 'vitest';
import { waitFor } from '@testing-library/svelte';
import { lazyChatFeedback } from './lazy-chat-feedback';
import type { FeedbackSurfaceBinding } from './types';
import type { FeedbackSurfaceProvider } from './context';

const state = vi.hoisted(() => ({ account: 0, create: vi.fn() }));
vi.mock('$lib/document-coordination', () => ({
  documentCoordination: () => ({ capture: () => {
    const account = state.account;
    return { assertCurrent: () => { if (account !== state.account) throw new Error('Account replaced'); } };
  } }),
}));
vi.mock('./chat-text-feedback', () => ({ chatTextFeedback: state.create }));
afterEach(() => { document.getSelection()?.removeAllRanges(); document.body.replaceChildren(); vi.clearAllMocks(); });

function setup() {
  const root = document.createElement('div'); root.textContent = 'Selected source'; document.body.append(root);
  const surface: FeedbackSurfaceProvider = { binding: null, available: true, open: vi.fn(), commenting: vi.fn() };
  const options = { surface, binding: null as FeedbackSurfaceBinding | null, key: crypto.randomUUID(), enabled: true, text: root.textContent };
  const mounted = { update: vi.fn(), resume: vi.fn(), destroy: vi.fn() };
  state.create.mockReturnValue(mounted);
  const action = lazyChatFeedback(root, options);
  const range = document.createRange(); range.setStart(root.firstChild!, 0); range.setEnd(root.firstChild!, 8);
  document.getSelection()!.addRange(range);
  document.dispatchEvent(new Event('selectionchange'));
  return { root, options, mounted, action };
}

describe('initial lazy import replacement', () => {
  it.each(['source', 'context', 'account', 'unmount'] as const)('discards an in-flight selection after %s changes', async change => {
    const { root, options, action } = setup();
    if (change === 'source') action.update({ ...options, text: 'New source', key: crypto.randomUUID() });
    if (change === 'context') action.update({ ...options, key: crypto.randomUUID() });
    if (change === 'account') state.account++;
    if (change === 'unmount') { action.destroy(); root.remove(); }
    await new Promise(resolve => setTimeout(resolve, 30));
    expect(state.create).not.toHaveBeenCalled();
    action.destroy();
  });
  it('mounts a new explicit open waiting behind the old import, without replaying old intent', async () => {
    const { options, mounted, action } = setup();
    const binding = {} as FeedbackSurfaceBinding;
    const next = { ...options, binding, key: crypto.randomUUID(), text: 'New source' };
    action.update(next);
    await waitFor(() => expect(state.create).toHaveBeenCalledOnce());
    expect(state.create.mock.calls[0][1]).toMatchObject({ key: next.key, text: next.text, binding });
    expect(mounted.resume).not.toHaveBeenCalled();
    action.destroy();
    expect(mounted.destroy).toHaveBeenCalledOnce();
  });
});
