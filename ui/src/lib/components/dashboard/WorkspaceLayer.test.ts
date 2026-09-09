import { fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { beforeAll, describe, expect, it, vi } from 'vitest';

import { WorkspaceManager } from '$lib/dashboard/workspace';

vi.mock('./WorkspaceWindow.svelte', async () => (
  import('./WorkspaceLayer.window-test-fixture.svelte')
));
vi.mock('./DashboardEntityModal.svelte', async () => (
  import('./WorkspaceLayer.entity-test-fixture.svelte')
));
vi.mock('./WorkspaceDock.svelte', async () => (
  import('./WorkspaceLayer.dock-test-fixture.svelte')
));

import WorkspaceLayer from './WorkspaceLayer.svelte';

const safe = { x: 0, y: 0, width: 1200, height: 800 };

function openConversation(manager: WorkspaceManager, id: string): void {
  manager.open({
    kind: 'conversation',
    entityId: id,
    title: `Conversation ${id}`,
    canonicalHref: `/chat/${id}`,
  });
}

describe('WorkspaceLayer stable window identity', () => {
  beforeAll(() => {
    vi.stubGlobal('ResizeObserver', class {
      observe() {}
      disconnect() {}
    });
    vi.stubGlobal('MutationObserver', class {
      observe() {}
      disconnect() {}
    });
    Object.defineProperty(window, 'matchMedia', {
      configurable: true,
      value: () => ({
        matches: false,
        addEventListener() {},
        removeEventListener() {},
      }),
    });
  });

  it('preserves keyed window and chat state through focus z-order changes', async () => {
    const manager = new WorkspaceManager('desktop', safe);
    openConversation(manager, 'a');
    openConversation(manager, 'b');
    render(WorkspaceLayer, { manager, deviceClass: 'desktop' });

    const windowA = await screen.findByTestId('workspace-window-conversation:a');
    const windowB = screen.getByTestId('workspace-window-conversation:b');
    const draftA = screen.getByTestId('workspace-chat-draft-a');
    await fireEvent.input(draftA, { target: { value: 'A draft' } });
    const instanceA = windowA.dataset.instanceId;
    const instanceB = windowB.dataset.instanceId;

    for (let index = 0; index < 5; index += 1) {
      manager.focus(index % 2 === 0 ? 'conversation:a' : 'conversation:b');
    }

    await waitFor(() => {
      expect(screen.getByTestId('workspace-window-conversation:a')).toBe(windowA);
      expect(screen.getByTestId('workspace-window-conversation:b')).toBe(windowB);
    });
    expect(windowA).toHaveAttribute('data-instance-id', instanceA);
    expect(windowB).toHaveAttribute('data-instance-id', instanceB);
    expect(draftA).toHaveValue('A draft');
    expect(manager.snapshot().map((window) => window.key)).toEqual([
      'conversation:a',
      'conversation:b',
    ]);
  });

  it('retains mounted identity through minimize, dashboard restore, and removes it on close', async () => {
    const manager = new WorkspaceManager('desktop', safe);
    openConversation(manager, 'a');
    openConversation(manager, 'b');
    render(WorkspaceLayer, { manager, deviceClass: 'desktop' });

    const windowA = await screen.findByTestId('workspace-window-conversation:a');
    const draftA = screen.getByTestId('workspace-chat-draft-a');
    await fireEvent.input(draftA, { target: { value: 'retained' } });

    manager.minimize('conversation:a');
    await waitFor(() => expect(windowA).toHaveAttribute('data-minimized', 'true'));
    manager.restore('conversation:a');
    await waitFor(() => expect(windowA).toHaveAttribute('data-minimized', 'false'));

    manager.showDashboard();
    await waitFor(() => expect(windowA).toHaveAttribute('data-minimized', 'true'));
    manager.restoreDashboard();
    await waitFor(() => expect(windowA).toHaveAttribute('data-minimized', 'false'));
    expect(screen.getByTestId('workspace-window-conversation:a')).toBe(windowA);
    expect(draftA).toHaveValue('retained');

    manager.close('conversation:a');
    await waitFor(() => {
      expect(screen.queryByTestId('workspace-window-conversation:a')).not.toBeInTheDocument();
    });
  });
});
