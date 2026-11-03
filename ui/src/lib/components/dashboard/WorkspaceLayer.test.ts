import { fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { beforeAll, describe, expect, it, vi } from 'vitest';

import { WorkspaceManager } from '$lib/dashboard/workspace';
import type { Agent } from '$lib/types/api';

vi.mock('./WorkspaceWindow.svelte', async () => (
  import('./WorkspaceLayer.window-test-fixture.svelte')
));
vi.mock('./DashboardEntityModal.svelte', async () => (
  import('./WorkspaceLayer.entity-test-fixture.svelte')
));
vi.mock('./AgentQuickChatModal.svelte', async () => (
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

  it.each(['desktop', 'tablet'] as const)(
    'consumes the first background click and minimizes all %s windows with a restorable snapshot',
    async (deviceClass) => {
      const manager = new WorkspaceManager(deviceClass, safe);
      openConversation(manager, 'a');
      openConversation(manager, 'b');
      render(WorkspaceLayer, { manager, deviceClass });
      const windowA = await screen.findByTestId('workspace-window-conversation:a');
      const draftA = screen.getByTestId('workspace-chat-draft-a');
      await fireEvent.input(draftA, { target: { value: 'retained' } });
      const backgroundClick = vi.fn();
      window.addEventListener('click', backgroundClick);
      try {
        const backdrop = screen.getByTestId('workspace-dismiss-backdrop');
        expect(await fireEvent.pointerDown(backdrop)).toBe(false);
        expect(manager.snapshot().every((window) => !window.minimized)).toBe(true);
        await fireEvent.click(backdrop);

        expect(backgroundClick).not.toHaveBeenCalled();
        expect(manager.snapshot().every((window) => window.minimized)).toBe(true);
        expect(manager.hasDashboardSnapshot()).toBe(true);
        await waitFor(() => {
          expect(screen.queryByTestId('workspace-dismiss-backdrop')).not.toBeInTheDocument();
        });

        manager.restoreDashboard();
        await waitFor(() => expect(windowA).toHaveAttribute('data-minimized', 'false'));
        expect(screen.getByTestId('workspace-window-conversation:a')).toBe(windowA);
        expect(draftA).toHaveValue('retained');
      } finally {
        window.removeEventListener('click', backgroundClick);
      }
    },
  );

  it('does not intercept the background when no windows are visible', () => {
    const manager = new WorkspaceManager('desktop', safe);
    openConversation(manager, 'a');
    manager.minimize('conversation:a');
    render(WorkspaceLayer, { manager, deviceClass: 'desktop' });
    expect(screen.queryByTestId('workspace-dismiss-backdrop')).not.toBeInTheDocument();
  });

  it.each([undefined, 'agent-stale-metadata'])(
    'renders the canonical agent entity with optional agent metadata %s',
    async (agentId) => {
      const agent: Agent = {
        agent_id: 'agent-canonical', owner_email: 'owner@example.test',
        name: 'Canonical agent', display_name: null, description: null,
        system_prompt: null, personality: null, skills: null, tools: null,
        permissions: null, llm_config: null, execution: null,
        personality_synced: false, personality_sync_error: null,
        personality_sync_checked_at: null, avatar_url: null, avatar_image_id: null,
        agent_type: 'primary', is_system: false, hidden: false,
        editable_fields: [], has_overrides: false, disabled: false, disableable: true,
        sync_metadata: null, is_shared_with_me: false, shared_by_email: null,
        granted_permission: null, executor_scope: null, is_readonly_for_caller: false,
        status: 'idle', created_at: null, updated_at: null,
      };
      const manager = new WorkspaceManager('desktop', safe);
      manager.open({
        kind: 'agent', entityId: agent.agent_id, title: agent.name,
        canonicalHref: `/chat/new?agent=${agent.agent_id}`,
        ...(agentId ? { agentId } : {}),
      });
      render(WorkspaceLayer, { manager, deviceClass: 'desktop', agents: [agent] });
      const draft = await screen.findByTestId('workspace-chat-draft-agent-canonical');
      await fireEvent.input(draft, { target: { value: 'retained agent draft' } });
      manager.minimize('agent:agent-canonical');
      manager.restore('agent:agent-canonical');
      await waitFor(() => expect(draft).toHaveValue('retained agent draft'));
      expect(screen.getByTestId('workspace-chat-draft-agent-canonical')).toBe(draft);
    },
  );

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
