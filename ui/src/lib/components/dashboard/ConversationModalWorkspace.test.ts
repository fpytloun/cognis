import { fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { tick } from 'svelte';

import ConversationModalWorkspace from './ConversationModalWorkspace.svelte';
const requests = vi.hoisted(() => ({
  detail: vi.fn(), managedAction: vi.fn(), activityOverview: vi.fn(), goto: vi.fn(),
}));
vi.mock('$app/navigation', () => ({ goto: requests.goto }));
vi.mock('$lib/stores/confirm', () => ({ confirmAction: vi.fn(async () => true) }));
vi.mock('$lib/chat-v2/api', () => ({ chatV2Api: { activityOverview: requests.activityOverview } }));

vi.mock('$lib/api/client', () => ({
  api: {
    conversations: {
      detail: requests.detail,
      managedAction: requests.managedAction,
      sessions: vi.fn(async () => [
        {
          session_id: 'session-root',
          conversation_id: 'conversation-one',
          parent_session_id: null,
          previous_session_id: 'session-predecessor',
          activity_scope_id: 'scope-one',
        },
        {
          session_id: 'session-predecessor',
          conversation_id: 'conversation-one',
          parent_session_id: null,
          previous_session_id: null,
          activity_scope_id: 'scope-one',
        },
      ]),
    },
  },
}));
vi.mock('$lib/components/chat-v2/CompactConversationChat.svelte', async () => (
  import('./ConversationModalWorkspace.chat-test-fixture.svelte')
));
vi.mock('$lib/components/inspector/SharedInspectorTabs.svelte', async () => (
  import('./ConversationModalWorkspace.inspector-test-fixture.svelte')
));
vi.mock('$lib/components/session/SessionDetailsButton.svelte', async () => (
  import('./ConversationModalWorkspace.session-button-test-fixture.svelte')
));

let observedWidth = 1000;
const resizeCallbacks = new Set<ResizeObserverCallback>();

describe('ConversationModalWorkspace inspector', () => {
  beforeEach(() => {
    requests.detail.mockReset();
    requests.managedAction.mockReset();
    requests.goto.mockReset();
    requests.activityOverview.mockReset().mockResolvedValue({ workstreams: [] });
  });
  beforeAll(() => {
    vi.stubGlobal('ResizeObserver', class {
      constructor(private readonly callback: ResizeObserverCallback) {}

      observe(target: Element): void {
        resizeCallbacks.add(this.callback);
        this.callback([{
          target,
          contentRect: { width: observedWidth },
        } as unknown as ResizeObserverEntry], this as unknown as ResizeObserver);
      }

      disconnect(): void {
        resizeCallbacks.delete(this.callback);
      }

      unobserve(): void {}
    });
    Object.defineProperty(HTMLElement.prototype, 'setPointerCapture', {
      configurable: true,
      value: vi.fn(),
    });
    Object.defineProperty(HTMLElement.prototype, 'releasePointerCapture', {
      configurable: true,
      value: vi.fn(),
    });
    Object.defineProperty(HTMLElement.prototype, 'hasPointerCapture', {
      configurable: true,
      value: () => true,
    });
  });

  async function expandManagedActions(): Promise<void> {
    const toggle = await screen.findByRole('button', { name: 'Actions' });
    if (toggle.getAttribute('aria-expanded') === 'false') await fireEvent.click(toggle);
  }

  it('uses the canonical root backing sessions, including successors of the selected session', async () => {
    requests.activityOverview.mockResolvedValue({ workstreams: [{
      key: 'root', root_key: 'root', session_id: 'session-next',
      backing_session_ids: ['session-predecessor', 'session-root', 'session-next'],
    }] });
    const scopeChanged = vi.fn();
    render(ConversationModalWorkspace, {
      conversationId: 'conversation-one', sessionId: 'session-root', onDisplayedScopeChange: scopeChanged,
    });
    await waitFor(() => expect(screen.getByTestId('compact-chat-controller-sessions')).toHaveTextContent('session-predecessor,session-root,session-next'));
    expect(scopeChanged).toHaveBeenLastCalledWith(expect.objectContaining({ key: 'conversation:conversation-one' }), 'session-next');
  });

  it('refreshes managed controls when a selected conversation becomes idle', async () => {
    const managed = (turn_state: string) => ({
      conversation_id: 'conversation-managed-a', has_active_turn: turn_state === 'running',
      managed_agent: { conversation_state: 'open', turn_state },
    });
    requests.detail.mockResolvedValue(managed('running'));
    render(ConversationModalWorkspace, { conversationId: 'conversation-parent', sessionId: 'root' });
    await fireEvent.click(screen.getByRole('button', { name: 'View conversation-managed-a' }));
    await expandManagedActions();
    await waitFor(() => expect(screen.getByRole('button', { name: 'Continue' })).toBeDisabled());
    expect(screen.getByTestId('compact-chat-scope')).toHaveTextContent('conversation:conversation-managed-a');
    requests.detail.mockResolvedValue(managed('idle'));
    await fireEvent.click(screen.getByRole('button', { name: 'Runtime idle' }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Continue' })).not.toBeDisabled());
  });

  it('opens the take-control follow-up when the managed conversation remains selected', async () => {
    requests.detail.mockImplementation(async (id: string) => ({
      conversation_id: id, has_active_turn: false,
      managed_agent: { conversation_state: 'open', turn_state: 'idle' },
    }));
    requests.managedAction.mockResolvedValue({
      managed_agent: { conversation_state: 'closed', turn_state: 'idle' },
      result: { conversation_id: 'conversation-follow-up-a' },
    });
    render(ConversationModalWorkspace, { conversationId: 'conversation-parent', sessionId: 'root' });
    await fireEvent.click(screen.getByRole('button', { name: 'View conversation-managed-a' }));
    await expandManagedActions();
    await waitFor(() => expect(screen.getByRole('button', { name: 'Take control' })).not.toBeDisabled());
    await fireEvent.click(screen.getByRole('button', { name: 'Take control' }));
    await waitFor(() => expect(requests.goto).toHaveBeenCalledWith('/chat/conversation-follow-up-a'));
  });

  it('restores parent conversations in the workspace rather than navigating to the same URL', async () => {
    requests.detail.mockImplementation(async (id: string) => ({
      conversation_id: id, has_active_turn: true,
      managed_agent: {
        conversation_state: 'open', turn_state: 'running',
        controller_conversation_id: id === 'conversation-managed-b' ? 'conversation-managed-a' : 'conversation-parent',
      },
    }));
    render(ConversationModalWorkspace, { conversationId: 'conversation-parent', sessionId: 'root' });
    await fireEvent.click(screen.getByRole('button', { name: 'View conversation-managed-a' }));
    await waitFor(() => expect(screen.getByRole('link', { name: 'Parent conversation' })).toHaveAttribute('href', '/chat/conversation-parent'));
    await fireEvent.click(screen.getByRole('button', { name: 'View conversation-managed-b' }));
    await waitFor(() => expect(screen.getByRole('link', { name: 'Parent conversation' })).toHaveAttribute('href', '/chat/conversation-managed-a'));
    await fireEvent.click(screen.getByRole('link', { name: 'Parent conversation' }));
    await waitFor(() => expect(screen.getByTestId('compact-chat-scope')).toHaveTextContent('conversation:conversation-managed-a'));
    await waitFor(() => expect(screen.getByRole('link', { name: 'Parent conversation' })).toHaveAttribute('href', '/chat/conversation-parent'));
    await fireEvent.click(screen.getByRole('link', { name: 'Parent conversation' }));
    await waitFor(() => expect(screen.getByTestId('compact-chat-scope')).toHaveTextContent('conversation:conversation-parent'));
    expect(screen.queryByTestId('managed-conversation-controls')).not.toBeInTheDocument();
    expect(requests.goto).not.toHaveBeenCalled();
  });

  it.each(['Continue', 'Take control'])('does not apply delayed %s to a subsequently selected conversation', async (actionLabel) => {
    requests.detail.mockImplementation(async (id: string) => ({
      conversation_id: id, has_active_turn: false,
      managed_agent: { conversation_state: 'open', turn_state: 'idle' },
    }));
    let complete!: (response: unknown) => void;
    requests.managedAction.mockReturnValue(new Promise(resolve => { complete = resolve; }));
    render(ConversationModalWorkspace, { conversationId: 'conversation-parent', sessionId: 'root' });
    await fireEvent.click(screen.getByRole('button', { name: 'View conversation-managed-a' }));
    await expandManagedActions();
    await waitFor(() => expect(screen.getByRole('button', { name: 'Continue' })).not.toBeDisabled());
    await fireEvent.click(screen.getByRole('button', { name: actionLabel }));
    await waitFor(() => expect(requests.managedAction).toHaveBeenCalledOnce());
    await fireEvent.click(screen.getByRole('button', { name: 'View conversation-managed-b' }));
    await expandManagedActions();
    await waitFor(() => expect(screen.getByRole('button', { name: 'Continue' })).not.toBeDisabled());
    complete({ managed_agent: { conversation_state: 'open', turn_state: 'running' }, result: { conversation_id: 'conversation-follow-up-a' } });
    await tick();
    expect(screen.getByTestId('compact-chat-scope')).toHaveTextContent('conversation:conversation-managed-b');
    expect(screen.getByRole('button', { name: 'Continue' })).not.toBeDisabled();
    expect(requests.goto).not.toHaveBeenCalled();
  });

  it('does not reopen a child when its pending lookup completes after restoring the parent', async () => {
    requests.detail.mockImplementation(async (id: string) => ({
      conversation_id: id, has_active_turn: false,
      managed_agent: { conversation_state: 'open', turn_state: 'idle', controller_conversation_id: 'conversation-parent' },
    }));
    render(ConversationModalWorkspace, { conversationId: 'conversation-parent', sessionId: 'root' });
    await fireEvent.click(screen.getByRole('button', { name: 'View conversation-managed-a' }));
    await screen.findByRole('link', { name: 'Parent conversation' });
    let complete!: (response: unknown) => void;
    requests.activityOverview.mockReset().mockReturnValue(new Promise(resolve => { complete = resolve; }));
    await fireEvent.click(screen.getByRole('button', { name: 'View unresolved session' }));
    await waitFor(() => expect(requests.activityOverview).toHaveBeenCalledOnce());
    await fireEvent.click(screen.getByRole('link', { name: 'Parent conversation' }));
    await waitFor(() => expect(screen.getByTestId('compact-chat-scope')).toHaveTextContent('conversation:conversation-parent'));
    complete({ workstreams: [{
      key: 'session:session-delayed', kind: 'delegate', parent_key: 'session:root',
      session_id: 'session-delayed', conversation_id: 'conversation-managed-a',
      agent_id: 'agent-1', title: 'Delayed child', status: 'completed',
    }] });
    await tick();
    expect(screen.getByTestId('compact-chat-scope')).toHaveTextContent('conversation:conversation-parent');
    expect(screen.queryByTestId('managed-conversation-controls')).not.toBeInTheDocument();
    expect(requests.goto).not.toHaveBeenCalled();
  });

  beforeEach(() => {
    observedWidth = 1000;
    window.localStorage.clear();
  });

  function resizeContainers(width: number): void {
    observedWidth = width;
    for (const callback of resizeCallbacks) {
      callback([{
        target: document.body,
        contentRect: { width },
      } as unknown as ResizeObserverEntry], {} as ResizeObserver);
    }
  }

  it('toggles a header-controlled desktop inspector without remounting chat', async () => {
    const first = render(ConversationModalWorkspace, {
      conversationId: 'conversation-one',
      inspectorControlInHeader: true,
    });

    const firstDraft = screen.getByTestId('compact-chat-draft-conversation-one');
    await fireEvent.input(firstDraft, { target: { value: 'preserved draft' } });

    expect(screen.queryByTestId('conversation-mobile-inspector-control')).not.toBeInTheDocument();
    await first.component.toggleInspector(false);

    expect(screen.queryByTestId('dashboard-conversation-inspector-conversation-one-desktop')).not.toBeInTheDocument();
    expect(screen.getByTestId('compact-chat-draft-conversation-one')).toBe(firstDraft);
    expect(firstDraft).toHaveValue('preserved draft');

    await first.component.toggleInspector(true);
    expect(screen.getByTestId('dashboard-conversation-inspector-conversation-one-desktop')).toBeInTheDocument();
    expect(first.component.getInspectorState()).toEqual({
      open: true,
      controlsId: 'dashboard-conversation-inspector-conversation-one',
    });
  });

  it('switches narrow containers to a collapsed overlay without unmounting chat', async () => {
    observedWidth = 700;
    const view = render(ConversationModalWorkspace, { conversationId: 'conversation-mobile' });

    await waitFor(() => expect(screen.getByTestId('conversation-modal-workspace')).toHaveAttribute('data-layout', 'narrow'));
    expect(screen.getByTestId('dashboard-conversation-chat')).toBeInTheDocument();
    await view.component.toggleInspector(true);
    expect(screen.getByTestId('dashboard-conversation-chat')).toBeInTheDocument();
    const mobileInspector = screen.getByTestId('dashboard-conversation-inspector-conversation-mobile-mobile');
    expect(mobileInspector).toBeInTheDocument();
    expect(mobileInspector).toHaveClass('inset-0', 'isolate', 'w-full', 'bg-canvas');
    expect(mobileInspector).not.toHaveClass('bg-canvas/98');
    await waitFor(() => expect(document.activeElement).toBe(mobileInspector));
    expect(screen.getByTestId('dashboard-conversation-chat')).toHaveAttribute('aria-hidden', 'true');

    await fireEvent.keyDown(mobileInspector, { key: 'Escape' });
    await waitFor(() => {
      expect(screen.queryByTestId('dashboard-conversation-inspector-conversation-mobile-mobile')).not.toBeInTheDocument();
    });
    expect(document.activeElement).toBe(
      screen.getByTestId('dashboard-conversation-inspector-conversation-mobile-mobile-toggle'),
    );

    await view.component.toggleInspector(true);

    resizeContainers(1000);
    await waitFor(() => expect(screen.getByTestId('conversation-modal-workspace')).toHaveAttribute('data-layout', 'wide'));
    expect(screen.queryByTestId('dashboard-conversation-inspector-conversation-mobile-mobile')).not.toBeInTheDocument();
    expect(screen.getByTestId('dashboard-conversation-inspector-conversation-mobile-desktop')).toBeInTheDocument();

    resizeContainers(700);
    await waitFor(() => expect(screen.getByTestId('conversation-modal-workspace')).toHaveAttribute('data-layout', 'narrow'));
    expect(screen.queryByTestId('dashboard-conversation-inspector-conversation-mobile-mobile')).not.toBeInTheDocument();
    expect(screen.getByTestId('dashboard-conversation-chat')).toBeInTheDocument();
  });

  it('uses a compact tablet inspector and supports independent keyboard resizing', async () => {
    const view = render(ConversationModalWorkspace, { conversationId: 'conversation-resize' });
    await view.component.toggleInspector(true);
    const inspector = await screen.findByTestId('dashboard-conversation-inspector-conversation-resize-desktop');
    const resizer = screen.getByTestId('dashboard-conversation-inspector-conversation-resize-desktop-resizer');

    expect(inspector).toHaveStyle({ width: '352px' });
    await fireEvent.keyDown(resizer, { key: 'ArrowLeft' });
    expect(inspector).toHaveStyle({ width: '362px' });
    expect(window.localStorage.getItem('cognis.dashboardConversationInspector.width.v1')).toBe('362');
    expect(resizer).toHaveClass('touch-resize-handle', 'touch-resize-handle--left');
  });

  it('migrates the former default inspector width to the compact default', async () => {
    window.localStorage.setItem('cognis.dashboardConversationInspector.width.v1', '400');
    const view = render(ConversationModalWorkspace, { conversationId: 'conversation-migrated' });
    await view.component.toggleInspector(true);
    expect(await screen.findByTestId('dashboard-conversation-inspector-conversation-migrated-desktop'))
      .toHaveStyle({ width: '352px' });
  });

  it('switches to a selected activity session and returns to the parent scope', async () => {
    render(ConversationModalWorkspace, {
      conversationId: 'conversation-one',
      sessionId: 'session-root',
    });

    expect(screen.getByTestId('compact-chat-scope')).toHaveTextContent('conversation:conversation-one');
    await waitFor(() => {
      expect(screen.getByTestId('compact-chat-controller-sessions'))
        .toHaveTextContent('session-root,session-predecessor');
    });
    await fireEvent.click(screen.getByRole('button', { name: 'Rotate session' }));
    await waitFor(() => {
      expect(screen.getByTestId('compact-chat-controller-sessions'))
        .toHaveTextContent('session-rotated');
    });
    await fireEvent.click(screen.getByRole('button', { name: 'View child session' }));

    expect(screen.getByTestId('compact-chat-scope')).toHaveTextContent('session:session-child');
    expect(screen.getByTestId('dashboard-conversation-session-header')).toHaveTextContent('Child session');
    expect(screen.getByTestId('dashboard-conversation-chat')).toHaveClass('flex', 'flex-col');
    await fireEvent.click(screen.getByRole('link', { name: 'Back to parent conversation' }));
    expect(screen.getByTestId('compact-chat-scope')).toHaveTextContent('conversation:conversation-one');
  });
});
