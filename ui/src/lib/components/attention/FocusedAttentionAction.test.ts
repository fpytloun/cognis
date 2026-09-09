import { fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { AttentionActionDetail } from '$lib/types/api';
import FocusedAttentionAction from './FocusedAttentionAction.svelte';

const mocks = vi.hoisted(() => ({
  get: vi.fn(),
  resolve: vi.fn(),
  subscribe: vi.fn(() => vi.fn()),
}));

vi.mock('$lib/api/client', () => ({
  api: {
    attentionActions: {
      get: mocks.get,
      resolve: mocks.resolve,
    },
  },
  asApiError: (error: unknown) => error,
}));

vi.mock('$lib/ws/client', () => ({
  wsClient: {
    subscribe: mocks.subscribe,
  },
}));

function detail(overrides: Partial<AttentionActionDetail> = {}): AttentionActionDetail {
  return {
    action_id: 'attention-1',
    kind: 'escalation',
    status: 'pending',
    availability: 'actionable',
    title: 'Tool approval required',
    source: {
      notification_id: 'attention-1',
      conversation_id: 'conversation-1',
      managed_origin_conversation_id: null,
      task_id: 'task-1',
      step_name: null,
      step_run_id: null,
      session_id: null,
    },
    can_resolve: true,
    has_action_form: true,
    expires_at: null,
    revision: 1,
    convergence_id: 'attention-1:1',
    display: {
      message: 'Approve the tool.',
      tool_name: 'deploy',
      arguments_display: {},
      reasoning: null,
      risk: null,
      questions: [],
      required_fields: [],
      credential_id: null,
      credential_kind: null,
      credential_label: null,
      credential_scope: null,
      authorization_url: null,
      user_code: null,
      callback_mode: null,
      executor_name: null,
    },
    allowed_actions: [
      { action: 'approve', label: 'Approve', intent: 'primary', input: 'note' },
    ],
    ...overrides,
  };
}

describe('FocusedAttentionAction', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.clearAllMocks();
    mocks.subscribe.mockReturnValue(vi.fn());
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('reconciles and safely closes after an authoritative 404', async () => {
    mocks.get.mockRejectedValueOnce({ status: 404, message: 'Not found' });
    const onUnavailable = vi.fn();
    const onDismiss = vi.fn();

    render(FocusedAttentionAction, {
      actionId: 'attention-1',
      onSettled: vi.fn(),
      onUnavailable,
      onDismiss,
    });

    await waitFor(() => expect(onUnavailable).toHaveBeenCalledWith('attention-1'));
    expect(screen.getByRole('alert')).toHaveTextContent('no longer available');
    expect(mocks.resolve).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1_000);
    expect(onDismiss).toHaveBeenCalledOnce();
  });

  it.each([
    ['resolved', 'resolved'],
    ['expired', 'expired'],
    ['unsupported', 'pending'],
  ] as const)('reconciles a %s detail without exposing a fake action', async (availability, status) => {
    const next = detail({
      kind: availability === 'unsupported' ? 'unsupported' : 'escalation',
      status,
      availability,
      can_resolve: false,
      has_action_form: false,
      allowed_actions: [],
    });
    mocks.get.mockResolvedValueOnce(next);
    const onSettled = vi.fn();
    const onUnavailable = vi.fn();
    const onDismiss = vi.fn();

    render(FocusedAttentionAction, {
      actionId: 'attention-1',
      onSettled,
      onUnavailable,
      onDismiss,
    });

    if (availability === 'resolved' || availability === 'expired') {
      await waitFor(() => expect(onSettled).toHaveBeenCalledWith(next));
    } else {
      await waitFor(() => expect(onUnavailable).toHaveBeenCalledWith('attention-1'));
      expect(screen.queryByRole('button', { name: 'Approve' })).not.toBeInTheDocument();
      await vi.advanceTimersByTimeAsync(1_000);
      expect(onDismiss).toHaveBeenCalledOnce();
    }
    expect(mocks.resolve).not.toHaveBeenCalled();
  });

  it('keeps a transport failure retryable and dismissible', async () => {
    mocks.get.mockRejectedValue({ status: 503, message: 'Service unavailable' });
    const onUnavailable = vi.fn();
    const onDismiss = vi.fn();

    render(FocusedAttentionAction, {
      actionId: 'attention-1',
      onSettled: vi.fn(),
      onUnavailable,
      onDismiss,
    });

    expect(await screen.findByRole('alert')).toHaveTextContent('Service unavailable');
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument();
    await fireEvent.click(screen.getByRole('button', { name: 'Close' }));
    expect(onDismiss).toHaveBeenCalledOnce();
    expect(onUnavailable).not.toHaveBeenCalled();
    expect(mocks.resolve).not.toHaveBeenCalled();
  });

  it('rejects an actionable-looking detail with no valid form', async () => {
    mocks.get.mockResolvedValueOnce(detail({ allowed_actions: [] }));
    const onUnavailable = vi.fn();

    render(FocusedAttentionAction, {
      actionId: 'attention-1',
      onSettled: vi.fn(),
      onUnavailable,
      onDismiss: vi.fn(),
    });

    await waitFor(() => expect(onUnavailable).toHaveBeenCalledWith('attention-1'));
    expect(screen.queryByRole('button', { name: 'Approve' })).not.toBeInTheDocument();
    expect(mocks.resolve).not.toHaveBeenCalled();
  });

  it('does not let a stale unavailable callback dismiss a reopened action', async () => {
    let releaseReconciliation!: () => void;
    const reconciliation = new Promise<void>((resolve) => {
      releaseReconciliation = resolve;
    });
    mocks.get
      .mockRejectedValueOnce({ status: 404, message: 'Not found' })
      .mockResolvedValueOnce(detail());
    const staleDismiss = vi.fn();
    const first = render(FocusedAttentionAction, {
      actionId: 'attention-1',
      onSettled: vi.fn(),
      onUnavailable: () => reconciliation,
      onDismiss: staleDismiss,
    });
    await waitFor(() => expect(mocks.get).toHaveBeenCalledTimes(1));
    first.unmount();

    render(FocusedAttentionAction, {
      actionId: 'attention-1',
      onSettled: vi.fn(),
      onUnavailable: vi.fn(),
      onDismiss: vi.fn(),
    });
    await waitFor(() => expect(screen.getByRole('button', { name: 'Approve' })).toBeVisible());
    releaseReconciliation();
    await reconciliation;
    await vi.advanceTimersByTimeAsync(1_000);

    expect(staleDismiss).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: 'Approve' })).toBeVisible();
  });

  it('does not let a stale resolution close a reopened action', async () => {
    let releaseResolution!: (value: {
      action_id: string;
      status: string;
      decision: string;
      revision: number;
      convergence_id: string;
    }) => void;
    const resolution = new Promise<{
      action_id: string;
      status: string;
      decision: string;
      revision: number;
      convergence_id: string;
    }>((resolve) => {
      releaseResolution = resolve;
    });
    mocks.get.mockResolvedValue(detail());
    mocks.resolve.mockReturnValueOnce(resolution);
    const staleSettled = vi.fn();
    const first = render(FocusedAttentionAction, {
      actionId: 'attention-1',
      onSettled: staleSettled,
      onUnavailable: vi.fn(),
      onDismiss: vi.fn(),
    });
    await fireEvent.click(await screen.findByRole('button', { name: 'Approve' }));
    await waitFor(() => expect(mocks.resolve).toHaveBeenCalledOnce());
    first.unmount();

    render(FocusedAttentionAction, {
      actionId: 'attention-1',
      onSettled: vi.fn(),
      onUnavailable: vi.fn(),
      onDismiss: vi.fn(),
    });
    await waitFor(() => expect(screen.getByRole('button', { name: 'Approve' })).toBeVisible());
    releaseResolution({
      action_id: 'attention-1',
      status: 'resolved',
      decision: 'approve',
      revision: 3,
      convergence_id: 'attention-1:3',
    });
    await resolution;

    expect(staleSettled).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: 'Approve' })).toBeVisible();
  });
});
