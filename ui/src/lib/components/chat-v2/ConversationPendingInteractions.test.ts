import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Notification } from '$lib/types/api';

import ConversationPendingInteractions from './ConversationPendingInteractions.svelte';

const mocks = vi.hoisted(() => ({
  list: vi.fn(),
  resolve: vi.fn(),
  wsListener: null as ((event: {
    type?: string;
    conversation_id?: string;
    task_id?: string;
    managed_origin_conversation_id?: string;
    payload?: Record<string, unknown>;
    reason?: string;
    notification_id?: string;
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
      task_id?: string;
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

function escalation(status = 'pending'): Notification {
  return {
    notification_id: 'notification-approval',
    notification_type: 'escalation',
    status,
    session_id: 'session-approval',
    task_id: null,
    step_name: null,
    step_run_id: null,
    resolution: null,
    resolved_at: null,
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
  it.each(['managed_conversation_completed', 'deliverable_ready'])(
    'does not paint an empty floating request container for %s notifications',
    async notification_type => {
      mocks.list.mockResolvedValue([{ ...escalation(), notification_type }]);
      render(ConversationPendingInteractions, { conversationId: 'conversation-one', presentation: 'floating' });
      await waitFor(() => expect(screen.queryByTestId('pending-interactions-loading')).not.toBeInTheDocument());
      expect(screen.queryByTestId('pending-conversation-interactions')).not.toBeInTheDocument();
      expect(mocks.resolve).not.toHaveBeenCalled();
    },
  );

  it('still renders an actionable request alongside a passive completion notification', async () => {
    mocks.list.mockResolvedValue([escalation(), {
      ...escalation(), notification_id: 'notification-completed', notification_type: 'managed_conversation_completed',
    }]);
    render(ConversationPendingInteractions, { conversationId: 'conversation-one', presentation: 'floating' });
    expect(await screen.findByRole('button', { name: 'Approve' })).toBeVisible();
    expect(screen.getByTestId('pending-conversation-interactions')).toBeVisible();
  });

  it('resolves task-scoped authentication without displaying unrelated request kinds', async () => {
    mocks.list.mockResolvedValueOnce([
      escalation(),
      {
        ...escalation(),
        task_id: 'task-auth',
        notification_id: 'notification-auth',
        notification_type: 'auth_challenge',
        payload: { label: 'VPN code', message: 'Enter the one-time code.', kind: 'otp_code', required_fields: ['code'] },
      },
    ]).mockResolvedValue([]);
    render(ConversationPendingInteractions, { taskId: 'task-auth', notificationTypes: ['auth_challenge'] });
    const input = await screen.findByRole('textbox');
    expect(screen.queryByRole('button', { name: 'Approve' })).not.toBeInTheDocument();
    expect(mocks.list).toHaveBeenCalledWith(null, { taskId: 'task-auth', signal: expect.any(AbortSignal) });
    await fireEvent.input(input, { target: { value: '492681' } });
    await fireEvent.click(screen.getByRole('button', { name: 'Send response' }));
    await waitFor(() => expect(mocks.resolve).toHaveBeenCalledWith('notification-auth', { decision: 'continue', response: '492681' }));
  });

  it('refreshes task requests only for the matching task', async () => {
    vi.useFakeTimers();
    mocks.list.mockResolvedValue([]);
    render(ConversationPendingInteractions, { taskId: 'task-auth', notificationTypes: ['auth_challenge'] });
    await vi.waitFor(() => expect(mocks.list).toHaveBeenCalledTimes(1));
    mocks.wsListener?.({ type: 'auth_challenge', task_id: 'another-task' });
    await vi.advanceTimersByTimeAsync(50);
    expect(mocks.list).toHaveBeenCalledTimes(1);
    mocks.wsListener?.({ type: 'auth_challenge', task_id: 'task-auth' });
    await vi.advanceTimersByTimeAsync(50);
    await vi.waitFor(() => expect(mocks.list).toHaveBeenCalledTimes(2));
  });

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

  it('owns and aborts read consumers on scope switch and unmount; late responses cannot publish or schedule work', async () => {
    const reads: Array<{ signal: AbortSignal; resolve: (value: unknown[]) => void }> = [];
    mocks.list.mockImplementation((_id: string, options: { signal: AbortSignal }) => new Promise(resolve => {
      reads.push({ signal: options.signal, resolve });
    }));
    const view = render(ConversationPendingInteractions, { conversationId: 'conversation-one' });
    await waitFor(() => expect(reads).toHaveLength(1));
    await view.rerender({ conversationId: 'conversation-two' });
    await waitFor(() => expect(reads).toHaveLength(2));
    expect(reads[0].signal.aborted).toBe(true);
    expect(reads[1].signal.aborted).toBe(false);
    reads[0].resolve([escalation()]);
    expect(screen.queryByRole('button', { name: 'Approval required · deploy_service' })).toBeNull();
    // Queue a WS refresh immediately before teardown; it must not create
    // another consumer after the scope's subscription has been released.
    mocks.wsListener?.({ type: 'escalation', conversation_id: 'conversation-two' });
    view.unmount();
    expect(reads[1].signal.aborted).toBe(true);
    reads[1].resolve([escalation()]);
    await new Promise(resolve => setTimeout(resolve, 100));
    expect(mocks.list).toHaveBeenCalledTimes(2);
    expect(screen.queryByRole('button', { name: 'Approval required · deploy_service' })).toBeNull();
  });

  it('shows safe tool approval details and dispatches the canonical decision', async () => {
    mocks.list.mockResolvedValueOnce([escalation()]).mockResolvedValueOnce([]);
    render(ConversationPendingInteractions, { conversationId: 'conversation-one' });

    expect(await screen.findByRole('button', { name: 'Approval required · deploy_service' })).toBeInTheDocument();
    expect(screen.getByText(/"environment": "staging"/)).toBeInTheDocument();
    expect(screen.getByText('Service interruption risk')).toBeInTheDocument();
    expect(screen.getByText('notification-approval')).toBeInTheDocument();
    expect(screen.getByText('/approve --id notification-approval')).toBeInTheDocument();
    expect(screen.getByText('/deny --id notification-approval')).toBeInTheDocument();
    expect(screen.getByText('Deployment changes external state.').closest('details')).toHaveAttribute('open');

    await fireEvent.click(screen.getByRole('button', { name: 'Approve' }));

    await waitFor(() => {
      expect(mocks.resolve).toHaveBeenCalledWith('notification-approval', {
        decision: 'approve',
        note: undefined,
      });
      expect(screen.queryByRole('button', { name: 'Approval required · deploy_service' })).not.toBeInTheDocument();
    });
  });

  it('shows bounded Gmail scope and the concrete reason without raw-payload expansion', async () => {
    const old = escalation();
    const item = {
      ...old,
      payload: {
        ...old.payload,
        tool_name: 'mcp_googleworkspace__search_gmail_messages',
        arguments_display: {
          query: 'after:2026/10/12 before:2026/10/14',
          page_size: 1,
          include_headers: false,
        },
        reasoning: 'Read at most one result to verify Gmail availability.',
        risk: 'low',
      },
    };
    mocks.list.mockResolvedValue([item]);
    render(ConversationPendingInteractions, { conversationId: 'conversation-one' });
    expect(await screen.findByText('after:2026/10/12 before:2026/10/14')).toBeInTheDocument();
    expect(screen.getByText('page size')).toBeInTheDocument();
    expect(screen.getByText('1')).toBeInTheDocument();
    expect(screen.getByText('include headers')).toBeInTheDocument();
    expect(screen.getByText('false')).toBeInTheDocument();
    expect(screen.getByText(item.payload.reasoning).closest('details')).toHaveAttribute('open');
    expect(mocks.resolve).not.toHaveBeenCalled();
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
      'bg-elevated',
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
    expect(screen.getByRole('button', { name: 'Approval required · deploy_service' })).toBeInTheDocument();
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
    await vi.waitFor(() => expect(screen.getByRole('button', { name: 'Approval required · deploy_service' })).toBeInTheDocument());

    mocks.wsListener?.({ type: 'escalation_resolved', conversation_id: 'conversation-one' });
    await vi.advanceTimersByTimeAsync(50);

    await vi.waitFor(() => expect(screen.queryByRole('button', { name: 'Approval required · deploy_service' })).not.toBeInTheDocument());
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

    expect(await screen.findByRole('button', { name: 'Approval required · deploy_service' })).toBeInTheDocument();
    expect(mocks.list).toHaveBeenCalledWith('conversation-one', { signal: expect.any(AbortSignal) });
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
    await vi.waitFor(() => expect(screen.getByRole('button', { name: 'Approval required · deploy_service' })).toBeInTheDocument());

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
    await vi.waitFor(() => expect(screen.getByRole('button', { name: 'Approval required · deploy_service' })).toBeInTheDocument());

    mocks.wsListener?.({
      type: 'escalation_resolved',
      conversation_id: 'conversation-child',
      managed_origin_conversation_id: 'conversation-one',
    });
    await vi.advanceTimersByTimeAsync(50);

    await vi.waitFor(() => expect(screen.queryByRole('button', { name: 'Approval required · deploy_service' })).not.toBeInTheDocument());
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
    await screen.findByRole('button', { name: 'Approval required · deploy_service' });

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
    const toggle = screen.getByRole('button', { name: 'Assistant requested more input' });
    await fireEvent.click(toggle);
    expect(screen.queryByRole('button', { name: 'Send response' })).toBeNull();
    expect(mocks.resolve).not.toHaveBeenCalled();
    await fireEvent.click(toggle);
    expect(screen.getByRole('button', { name: 'Send response' })).toBeEnabled();
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

  it('retires a question answered in another chat even when an older read finishes late', async () => {
    const question: Notification = {
      ...escalation(),
      notification_id: 'notification-question',
      notification_type: 'step_question',
      payload: { question: 'Choose a rollout mode.', questions: [{
        id: 'rollout', question: 'Choose a rollout mode.',
        options: [{ id: 'canary', label: 'Canary', description: null }],
        multiple: false, allow_custom: false, required: true,
      }] },
    };
    let finishOldRead!: (items: Notification[]) => void;
    mocks.list.mockResolvedValueOnce([question])
      .mockImplementationOnce(() => new Promise<Notification[]>(resolve => { finishOldRead = resolve; }))
      .mockResolvedValue([]);
    render(ConversationPendingInteractions, { conversationId: 'conversation-one' });
    expect(await screen.findByRole('button', { name: 'Send response' })).toBeInTheDocument();
    window.dispatchEvent(new Event('focus'));
    await waitFor(() => expect(finishOldRead).toBeDefined());
    mocks.wsListener?.({
      type: 'workflow_step_question_resolved', conversation_id: 'conversation-one',
      notification_id: question.notification_id,
    });
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Send response' })).toBeNull());
    finishOldRead([question]);
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Send response' })).toBeNull());
  });

  it('reconciles a missed question resolution from canonical state invalidation', async () => {
    mocks.list.mockResolvedValueOnce([escalation()]).mockResolvedValue([]);
    render(ConversationPendingInteractions, { conversationId: 'conversation-one' });
    expect(await screen.findByRole('button', { name: 'Approve' })).toBeInTheDocument();
    mocks.wsListener?.({ type: 'conversation_state_delta', conversation_id: 'conversation-one' });
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Approve' })).toBeNull());
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
