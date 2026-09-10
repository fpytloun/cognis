import { fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

import ConversationModalWorkspace from './ConversationModalWorkspace.svelte';

vi.mock('$lib/api/client', () => ({
  api: {
    conversations: {
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
    expect(mobileInspector).toHaveClass('inset-0', 'isolate', 'w-full', 'bg-slate-950');
    expect(mobileInspector).not.toHaveClass('bg-slate-950/98');
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
    await fireEvent.click(screen.getByRole('button', { name: 'View child session' }));

    expect(screen.getByTestId('compact-chat-scope')).toHaveTextContent('session:session-child');
    expect(screen.getByTestId('dashboard-conversation-session-header')).toHaveTextContent('Child session');
    expect(screen.getByTestId('dashboard-conversation-chat')).toHaveClass('flex', 'flex-col');
    await fireEvent.click(screen.getByRole('button', { name: 'Back to parent conversation' }));
    expect(screen.getByTestId('compact-chat-scope')).toHaveTextContent('conversation:conversation-one');
  });
});
