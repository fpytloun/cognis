import type { ContextUsage, GenerationPerformanceSnapshot } from '$lib/types/api';
import type { SessionInfoData } from '$lib/sessionInfoCache';

export interface SessionDiagnostics {
  ownerSessionId: string | null;
  contextUsage: ContextUsage | null;
  lastGeneration: GenerationPerformanceSnapshot | null;
}

export function diagnosticsForSession(
  sessionId: string | null,
  info: SessionInfoData | null,
): SessionDiagnostics {
  if (!sessionId || info?.intaris_session_id !== sessionId) {
    return { ownerSessionId: sessionId, contextUsage: null, lastGeneration: null };
  }
  return {
    ownerSessionId: sessionId,
    contextUsage: info.context_usage ?? null,
    lastGeneration: info.last_generation ?? null,
  };
}

export function acceptsSessionDiagnostics(
  focusedSessionId: string | null,
  activeSessionId: string | null,
  sourceSessionId: string | null,
): boolean {
  return sourceSessionId !== null && sourceSessionId === (focusedSessionId ?? activeSessionId);
}
