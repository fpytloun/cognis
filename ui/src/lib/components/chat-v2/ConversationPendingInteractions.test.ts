import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import ConversationPendingInteractions from './ConversationPendingInteractions.svelte';

const mocks = vi.hoisted(() => ({
  list: vi.fn(),
  resolve: vi.fn(),
  wsListener: null as ((event: {
    type?: string;
    conversation_id?: string;
    managed_origin_conversation_id?: string;
    payload?: Record<string, unknown>;
    reason?: string;
  }) => void) | null,
  wsStateListener: null as ((state: { status: string }) => void) | null,
  wsInitialState: 'idle' as 'idle' | 'connected',
}));

vi.mock('$lib/api/client', () => ({
  api: { notifications: { list: mocks.list, resolve: mocks.resolve } },
  asApiError: (error: unknown) => ({
    message: error instanceof Error ? error.message : 'Request failed',
  }),
}));

vi.mock('$lib/ws/client', () => ({
  wsClient: {
    subscribe: vi.fn((listener: (event: {
      type?: string;
      conversation_id?: string;
      managed_origin_conversation_id?: string;
      payload?: Record<string, unknown>;
      reason?: string;
    }) => void) => {
      mocks.wsListener = listener;
      return vi.fn();
    }),
  },
  wsState: {
    subscribe: vi.fn((listener: (state: { status: string }) => void) => {
      mocks.wsStateListener = listener;
      listener({ status: mocks.wsInitialState });
      return vi.fn();
    }),
  },
}));

function escalation(status = 'pending') {
  return {
    notification_id: 'notification-approval',
    notification_type: 'escalation',
    status,
    session_id: 'session-approval',
    task_id: null,
    conversation_id: 'conversation-one',
    created_at: new Date().toISOString(),
    payload: {
      tool_name: 'deploy_service',
      arguments_display: { environment: 'staging' },
      reasoning: 'Deployment changes external state.',
      risk: 'Service interruption',
      timeout_seconds: 300,
    },
  };
}

describe('ConversationPendingInteractions', () => {
  beforeEach(() => {
    vi.useRealTimers();
    vi.clearAllMocks();
    mocks.list.mockReset();
    mocks.resolve.mockReset();
    mocks.wsListener = null;
    mocks.wsStateListener = null;
    mocks.wsInitialState = 'idle';
    mocks.list.mockResolvedValue([]);
    mocks.resolve.mockResolvedValue({ ok: true });
  });

  afterEach(() => {
    cleanup();
    vi.useRealTimers();
  });

  it('shows safe tool approval details and dispatches the canonical decision', async () => {
    mocks.list.mockResolvedValueOnce([escalation()]).mockResolvedValueOnce([]);
    render(ConversationPendingInteractions, { conversationId: 'conversation-one' });

    expect(await screen.findByRole('heading', { name: 'deploy_service' })).toBeInTheDocument();
    expect(screen.getByText(/"environment": "staging"/)).toBeInTheDocument();
    expect(screen.getByText('Service interruption risk')).toBeInTheDocument();

    await fireEvent.click(screen.getByRole('button', { name: 'Approve' }));

    await waitFor(() => {
      expect(mocks.resolve).toHaveBeenCalledWith('notification-approval', {
        decision: 'approve',
        note: undefined,
      });
      expect(screen.queryByRole('heading', { name: 'deploy_service' })).not.toBeInTheDocument();
    });
  });

  it('uses an opaque bounded scroll surface only for floating presentation', async () => {
    mocks.list.mockResolvedValue([escalation()]);
    render(ConversationPendingInteractions, {
      conversationId: 'conversation-one',
      presentation: 'floating',
    });

    const panel = await screen.findByTestId('pending-conversation-interactions');
    expect(panel).toHaveAttribute('data-presentation', 'floating');
    expect(panel).toHaveClass(
      'max-h-full',
      'overflow-y-auto',
      'rounded-2xl',
      'border',
      'bg-slate-950',
      'shadow-2xl',
    );
    expect(panel).not.toHaveClass('max-h-[45%]', 'shrink-0');
  });

  it('keeps the request visible and reports a resolution error', async () => {
    mocks.list.mockResolvedValue([escalation()]);
    mocks.resolve.mockRejectedValueOnce(new Error('Approval failed'));
    render(ConversationPendingInteractions, { conversationId: 'conversation-one' });

    await fireEvent.click(await screen.findByRole('button', { name: 'Approve' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('Approval failed');
    expect(screen.getByRole('heading', { name: 'deploy_service' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Approve' })).toBeEnabled();
  });

  it('disables approval actions while resolution is pending', async () => {
    let finish: () => void = () => {};
    mocks.resolve.mockImplementation(() => new Promise<void>((done) => { finish = done; }));
    mocks.list.mockResolvedValue([escalation()]);
    render(ConversationPendingInteractions, { conversationId: 'conversation-one' });

    const approve = await screen.findByRole('button', { name: 'Approve' });
    await fireEvent.click(approve);

    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Submitting approval…' })).toBeDisabled();
    });
    finish();
  });

  it('refreshes and removes a resolved request after a conversation realtime event', async () => {
    vi.useFakeTimers();
    mocks.list.mockResolvedValueOnce([escalation()]).mockResolvedValueOnce([]);
    render(ConversationPendingInteractions, { conversationId: 'conversation-one' });
    await vi.waitFor(() => expect(screen.getByRole('heading', { name: 'deploy_service' })).toBeInTheDocument());

    mocks.wsListener?.({ type: 'escalation_resolved', conversation_id: 'conversation-one' });
    await vi.advanceTimersByTimeAsync(50);

    await vi.waitFor(() => expect(screen.queryByRole('heading', { name: 'deploy_service' })).not.toBeInTheDocument());
    expect(mocks.list).toHaveBeenCalledTimes(2);
  });

  it('hydrates a managed-origin escalation for the visible parent conversation', async () => {
    mocks.list.mockResolvedValueOnce([{
      ...escalation(),
      conversation_id: 'conversation-child',
      payload: {
        ...escalation().payload,
        managed_origin_conversation_id: 'conversation-one',
      },
    }]);
    render(ConversationPendingInteractions, { conversationId: 'conversation-one' });

    expect(await screen.findByRole('heading', { name: 'deploy_service' })).toBeInTheDocument();
    expect(mocks.list).toHaveBeenCalledWith('conversation-one');
  });

  it('refreshes for a managed-origin event but ignores unrelated child approvals', async () => {
    vi.useFakeTimers();
    mocks.list.mockResolvedValueOnce([]).mockResolvedValueOnce([escalation()]);
    render(ConversationPendingInteractions, { conversationId: 'conversation-one' });
    await vi.waitFor(() => expect(mocks.list).toHaveBeenCalledTimes(1));

    mocks.wsListener?.({
      type: 'escalation',
      conversation_id: 'conversation-child',
      managed_origin_conversation_id: 'conversation-one',
    });
    await vi.advanceTimersByTimeAsync(50);
    await vi.waitFor(() => expect(screen.getByRole('heading', { name: 'deploy_service' })).toBeInTheDocument());

    mocks.wsListener?.({
      type: 'escalation_resolved',
      conversation_id: 'conversation-unrelated-child',
      managed_origin_conversation_id: 'conversation-unrelated-parent',
    });
    await vi.advanceTimersByTimeAsync(50);

    expect(mocks.list).toHaveBeenCalledTimes(2);
  });

  it('removes a managed-origin escalation after its resolution event', async () => {
    vi.useFakeTimers();
    mocks.list.mockResolvedValueOnce([{
      ...escalation(),
      conversation_id: 'conversation-child',
      payload: {
        ...escalation().payload,
        managed_origin_conversation_id: 'conversation-one',
      },
    }]).mockResolvedValueOnce([]);
    render(ConversationPendingInteractions, { conversationId: 'conversation-one' });
    await vi.waitFor(() => expect(screen.getByRole('heading', { name: 'deploy_service' })).toBeInTheDocument());

    mocks.wsListener?.({
      type: 'escalation_resolved',
      conversation_id: 'conversation-child',
      managed_origin_conversation_id: 'conversation-one',
    });
    await vi.advanceTimersByTimeAsync(50);

    await vi.waitFor(() => expect(screen.queryByRole('heading', { name: 'deploy_service' })).not.toBeInTheDocument());
  });

  it('refetches after notification invalidation and WebSocket recovery', async () => {
    vi.useFakeTimers();
    mocks.list.mockResolvedValue([]);
    render(ConversationPendingInteractions, { conversationId: 'conversation-one' });
    await vi.waitFor(() => expect(mocks.list).toHaveBeenCalledTimes(1));

    mocks.wsListener?.({
      type: 'scope_invalidated',
      reason: 'notification_state_changed',
      conversation_id: 'conversation-one',
    });
    await vi.advanceTimersByTimeAsync(50);
    await vi.waitFor(() => expect(mocks.list).toHaveBeenCalledTimes(2));

    mocks.wsStateListener?.({ status: 'connected' });
    await vi.advanceTimersByTimeAsync(50);
    await vi.waitFor(() => expect(mocks.list).toHaveBeenCalledTimes(3));
  });

  it('does not duplicate initial hydration when the WebSocket is already connected', async () => {
    mocks.wsInitialState = 'connected';
    render(ConversationPendingInteractions, { conversationId: 'conversation-one' });

    await vi.waitFor(() => expect(mocks.list).toHaveBeenCalledTimes(1));
  });

  it('ignores ordinary realtime frames', async () => {
    mocks.list.mockResolvedValueOnce([escalation()]);
    render(ConversationPendingInteractions, { conversationId: 'conversation-one' });
    await screen.findByRole('heading', { name: 'deploy_service' });

    mocks.wsListener?.({ type: 'chunk', conversation_id: 'conversation-one' });
    await new Promise((done) => setTimeout(done, 75));

    expect(mocks.list).toHaveBeenCalledTimes(1);
  });

  it('renders a structured question and submits its canonical response payload', async () => {
    mocks.list
      .mockResolvedValueOnce([{
        ...escalation(),
        notification_id: 'notification-question',
        notification_type: 'step_question',
        payload: {
          question: 'Choose a rollout mode.',
          questions: [{
            id: 'rollout',
            question: 'Choose a rollout mode.',
            options: [{ id: 'canary', label: 'Canary', description: null }],
            multiple: false,
            allow_custom: false,
            required: true,
          }],
        },
      }])
      .mockResolvedValueOnce([]);
    render(ConversationPendingInteractions, { conversationId: 'conversation-one' });

    await fireEvent.click(await screen.findByText('Canary'));
    await fireEvent.click(screen.getByRole('button', { name: 'Send response' }));

    await waitFor(() => expect(mocks.resolve).toHaveBeenCalledWith(
      'notification-question',
      {
        decision: 'continue',
        response_payload: {
          answers: [{
            question_id: 'rollout',
            selected_option_ids: ['canary'],
            custom_answer: null,
          }],
        },
      },
    ));
  });

  it('exposes OAuth authorization without offering an invalid continue action', async () => {
    mocks.list.mockResolvedValueOnce([{
      ...escalation(),
      notification_id: 'notification-oauth',
      notification_type: 'auth_challenge',
      payload: {
        kind: 'oauth_authorization',
        message: 'Authorize the MCP provider.',
        metadata: { authorization_url: 'https://auth.example.test/authorize' },
      },
    }]);
    render(ConversationPendingInteractions, { conversationId: 'conversation-one' });

    const link = await screen.findByRole('link', { name: 'Open authorization' });
    expect(link).toHaveAttribute('href', 'https://auth.example.test/authorize');
    expect(screen.queryByRole('button', { name: 'Send response' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeInTheDocument();
  });

  it('does not render a response form for OAuth authorization without a usable URL', async () => {
    mocks.list.mockResolvedValueOnce([{
      ...escalation(),
      notification_id: 'notification-oauth-invalid',
      notification_type: 'auth_challenge',
      payload: {
        kind: 'oauth_authorization',
        message: 'Authorization is no longer available.',
        metadata: { authorization_url: 'not-a-url' },
      },
    }]);
    render(ConversationPendingInteractions, { conversationId: 'conversation-one' });

    expect(await screen.findByText('Authorization is no longer available.')).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'Open authorization' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Send response' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeInTheDocument();
  });

  it('renders device-code and executor-loopback OAuth instructions', async () => {
    mocks.list.mockResolvedValueOnce([{
      ...escalation(),
      notification_id: 'notification-device-code',
      notification_type: 'auth_challenge',
      payload: {
        kind: 'oauth_authorization',
        metadata: {
          verification_uri: 'https://auth.example.test/device',
          user_code: 'ABCD-1234',
          callback_mode: 'executor_loopback',
          oauth_executor_name: 'workstation-one',
        },
      },
    }]);
    render(ConversationPendingInteractions, { conversationId: 'conversation-one' });

    expect(await screen.findByText(/Open this URL on executor workstation-one/)).toBeInTheDocument();
    expect(screen.getByText('https://auth.example.test/device')).toBeInTheDocument();
    expect(screen.getByText('ABCD-1234')).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'Open authorization' })).not.toBeInTheDocument();
  });
});
