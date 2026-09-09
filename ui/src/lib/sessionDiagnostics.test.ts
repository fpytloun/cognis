import { describe, expect, it } from 'vitest';
import {
  acceptsSessionDiagnostics,
  diagnosticsForSession,
  mergeContextUsage,
} from './sessionDiagnostics';
import type { SessionInfoData } from './sessionInfoCache';

function detail(sessionId: string, promptTokens: number): SessionInfoData {
  return {
    intaris_session_id: sessionId,
    intention: null,
    summary: null,
    status: 'complete',
    total_calls: 0,
    approved_count: 0,
    denied_count: 0,
    escalated_count: 0,
    context_usage: {
      model: sessionId,
      prompt_tokens: promptTokens,
      max_context_tokens: 100,
      percentage: promptTokens,
      loop_pressure_threshold: 80,
      effective_prompt_budget: 90,
    },
    last_generation: {
      measured_at: `2026-08-07T00:00:0${promptTokens}Z`,
      model: sessionId,
    },
  } as SessionInfoData;
}

describe('session diagnostics ownership', () => {
  it('restores distinct cached diagnostics for A, B, and structural Back', () => {
    const sessionA = detail('session-a', 1);
    const sessionB = detail('session-b', 2);
    expect(diagnosticsForSession('session-a', sessionA).contextUsage?.prompt_tokens).toBe(1);
    expect(diagnosticsForSession('session-b', sessionB).contextUsage?.prompt_tokens).toBe(2);
    expect(diagnosticsForSession('session-a', sessionA).lastGeneration?.model).toBe('session-a');
  });

  it('suppresses missing or mismatched cached diagnostics', () => {
    expect(diagnosticsForSession('session-b', detail('session-a', 1))).toEqual({
      ownerSessionId: 'session-b',
      contextUsage: null,
      lastGeneration: null,
    });
    expect(diagnosticsForSession('session-b', null).contextUsage).toBeNull();
  });

  it('rejects stale session responses after focus changes', () => {
    expect(acceptsSessionDiagnostics('session-b', 'root-session', 'session-a')).toBe(false);
    expect(acceptsSessionDiagnostics('session-b', 'root-session', 'session-b')).toBe(true);
    expect(acceptsSessionDiagnostics(null, 'root-session', 'root-session')).toBe(true);
  });

  it('keeps the newest context snapshot for one session', () => {
    const current = {
      ...detail('session-a', 225).context_usage!,
      runtime_metadata_revision: 14,
    };
    const stale = {
      ...detail('session-a', 59).context_usage!,
      runtime_metadata_revision: 5,
    };
    expect(mergeContextUsage(current, stale)?.prompt_tokens).toBe(225);
    expect(mergeContextUsage(
      current,
      { ...current, prompt_tokens: 238, runtime_metadata_revision: 15 },
    )?.prompt_tokens).toBe(238);
  });

  it('does not let an unversioned response replace versioned context', () => {
    const current = {
      ...detail('session-a', 225).context_usage!,
      runtime_metadata_revision: 14,
    };
    expect(mergeContextUsage(
      current,
      detail('session-a', 59).context_usage ?? null,
    )?.prompt_tokens).toBe(225);
  });
});
