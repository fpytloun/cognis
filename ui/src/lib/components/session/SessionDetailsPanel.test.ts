import { cleanup, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';

import SessionDetailsPanel from './SessionDetailsPanel.svelte';

const { intarisDetail } = vi.hoisted(() => ({ intarisDetail: vi.fn() }));

vi.mock('$lib/api/client', () => ({
  api: { sessions: { intarisDetail } },
}));

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

function detail(sessionId: string) {
  return {
    session_id: sessionId,
    intaris_session_id: `intaris-${sessionId}`,
    title: 'Session',
    intention: 'Intent',
    summary: `Summary ${sessionId}`,
    status: 'active',
    total_calls: 7,
    approved_count: 5,
    denied_count: 1,
    escalated_count: 1,
    context_usage: {
      prompt_tokens: 1200,
      max_context_tokens: 8000,
      percentage: 15,
      model: 'model-a',
      reasoning_effort: 'medium',
      agent_profile_id: 'profile-a',
      provider_id: 'provider-a',
      effective_prompt_budget: 6000,
    },
    token_usage: {
      prompt_tokens: 1200,
      completion_tokens: 100,
      total_tokens: 1300,
    },
    last_generation: {
      is_local: true,
      provider_id: 'provider-a',
      provider_name: 'Provider A',
      runtime: 'ollama',
      location: 'executor',
      executor_id: 'executor-a',
      executor_name: 'Executor A',
      model: 'model-a',
      digest: null,
      quantization: 'Q4',
      configured_context_tokens: 8000,
      prompt_tokens: 1200,
      completion_tokens: 100,
      prompt_tokens_per_second: 50,
      generation_tokens_per_second: 20,
      time_to_first_token_seconds: 0.2,
      load_duration_seconds: 0.1,
      total_duration_seconds: 2,
      processor: 'GPU',
      gpu_residency: 'full',
      measured_at: '2026-01-01T00:00:00Z',
    },
  };
}

afterEach(() => {
  cleanup();
  intarisDetail.mockReset();
});

describe('SessionDetailsPanel', () => {
  it('renders full authorized session details', async () => {
    intarisDetail.mockResolvedValue(detail('session-a'));
    render(SessionDetailsPanel, { sessionId: 'session-a' });
    await waitFor(() => expect(screen.getAllByText('Summary session-a')).toHaveLength(2));
    expect(screen.getByTestId('session-narrative')).not.toHaveAttribute('open');
    expect(screen.getByText(/model-a · Provider A/)).toBeTruthy();
    expect(screen.getByText('profile-a')).toBeTruthy();
    expect(screen.getByText(/1,200 \/ 8,000/)).toBeTruthy();
    expect(screen.getByText('Executor A')).toBeTruthy();
    expect(screen.getByTestId('session-context-usage-bar')).toHaveAttribute('aria-valuenow', '15');
    expect(screen.getByTestId('session-details-diagnostics')).not.toHaveAttribute('open');
    expect(screen.getByText('Show context & runtime details')).toBeTruthy();
    expect(screen.getByTestId('session-token-usage')).toBeTruthy();
    expect(screen.getByText('Input 1,200')).toBeTruthy();
    expect(screen.getByText('Output 100')).toBeTruthy();
  });

  it('rejects a stale response after a scope switch', async () => {
    const first = deferred<ReturnType<typeof detail>>();
    const second = deferred<ReturnType<typeof detail>>();
    intarisDetail.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    const view = render(SessionDetailsPanel, { sessionId: 'session-a' });
    await view.rerender({ sessionId: 'session-b' });
    second.resolve(detail('session-b'));
    await waitFor(() => expect(screen.getAllByText('Summary session-b')).toHaveLength(2));
    first.resolve(detail('session-a'));
    await Promise.resolve();
    expect(screen.queryByText('Summary session-a')).toBeNull();
  });
});
