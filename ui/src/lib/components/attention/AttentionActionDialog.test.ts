import { fireEvent, render, waitFor } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import AttentionActionDialog from './AttentionActionDialog.svelte';

const navigation = vi.hoisted(() => ({
  beforeNavigate: vi.fn(),
  goto: vi.fn(),
  callback: null as null | ((navigation: {
    to: { url: URL; route: { id: string | null } };
    cancel: () => void;
  }) => void),
}));

const focused = vi.hoisted(() => ({
  get: vi.fn(() => new Promise(() => undefined)),
  subscribe: vi.fn(() => vi.fn()),
}));

vi.mock('$app/navigation', () => ({
  beforeNavigate: (callback: typeof navigation.callback) => {
    navigation.callback = callback;
    navigation.beforeNavigate(callback);
  },
  goto: navigation.goto,
}));

vi.mock('$lib/api/client', () => ({
  api: {
    attentionActions: {
      get: focused.get,
      resolve: vi.fn(),
    },
  },
  asApiError: (error: unknown) => error,
}));

vi.mock('$lib/ws/client', () => ({
  wsClient: {
    subscribe: focused.subscribe,
  },
}));

describe('AttentionActionDialog history lifecycle', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    navigation.callback = null;
    window.history.replaceState({}, '', '/');
  });

  afterEach(() => {
    window.history.replaceState({}, '', '/');
  });

  it('removes only its owned history entry on direct unmount', () => {
    const back = vi.spyOn(window.history, 'back').mockImplementation(() => undefined);
    const view = render(AttentionActionDialog, {
      actionId: 'attention-1',
      title: 'Tool approval required',
      onClose: vi.fn(),
      onSettled: vi.fn(),
    });

    expect(window.history.state.cognisAttentionActionId).toBe('attention-1');
    view.unmount();
    expect(back).toHaveBeenCalledOnce();
    back.mockRestore();
  });

  it('pops the sentinel before continuing an internal route transition', async () => {
    const back = vi.spyOn(window.history, 'back').mockImplementation(() => undefined);
    const onClose = vi.fn();
    render(AttentionActionDialog, {
      actionId: 'attention-1',
      title: 'Tool approval required',
      onClose,
      onSettled: vi.fn(),
    });
    const cancel = vi.fn();
    const target = new URL('/tasks/task-1', window.location.href);

    navigation.callback?.({
      to: { url: target, route: { id: '/tasks/[taskId]' } },
      cancel,
    });
    expect(cancel).toHaveBeenCalledOnce();
    expect(back).toHaveBeenCalledOnce();
    await fireEvent(window, new PopStateEvent('popstate'));

    await waitFor(() => expect(onClose).toHaveBeenCalledOnce());
    expect(navigation.goto).toHaveBeenCalledWith(target);
    back.mockRestore();
  });
});
