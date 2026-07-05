/**
 * L2 golden event-stream replay tests.
 *
 * Loads golden event streams captured by the pytest e2e tests
 * (tests/e2e/golden/<scenario>.jsonl) and replays them through the real
 * ChatTimeline store, asserting client-store invariants.
 *
 * This is the primary fast feedback loop for the "reproduce -> fix -> verify"
 * cycle.  It runs in milliseconds (no browser, no network) and deterministically
 * reproduces the exact event sequences that caused historical bugs.
 *
 * Invariants checked:
 *   INV-NO-HANG:             no streaming item after message_complete
 *   INV-NO-DUP:              no duplicate ids at any snapshot
 *   INV-MONOTONIC-PRESENCE:  no item disappears then reappears
 *   INV-STABLE-ORDERKEY:     orderKey never increases for a given id
 *   INV-FIELD-PRESERVE:      tool_call arguments survive follow-up patches
 *
 * Golden files are written by: uv run pytest tests/e2e/ -m e2e
 * If no golden files exist, these tests are skipped.
 */

import { describe, expect, it } from 'vitest';
import { existsSync, readdirSync, readFileSync } from 'fs';
import { join } from 'path';
import { ChatTimeline } from '$lib/chat-timeline.svelte';
import type { CognisWebSocketEvent } from '$lib/types/api';
import type { TimelineItem } from '$lib/chat';
import { checkAllInvariants, checkNoForeignSession } from '$lib/test-support/timeline-invariants';

// ---------------------------------------------------------------------------
// Golden file loading
// ---------------------------------------------------------------------------

const GOLDEN_DIR = join(process.cwd(), '..', 'tests', 'e2e', 'golden');

function loadGoldenFiles(): Array<{ scenario: string; events: CognisWebSocketEvent[] }> {
  if (!existsSync(GOLDEN_DIR)) {
    return [];
  }

  const files = readdirSync(GOLDEN_DIR).filter((f) => f.endsWith('.jsonl'));
  return files.map((file) => {
    const scenario = file.replace('.jsonl', '');
    const content = readFileSync(join(GOLDEN_DIR, file), 'utf-8');
    const events = content
      .trim()
      .split('\n')
      .filter((line) => line.trim())
      .map((line) => JSON.parse(line) as CognisWebSocketEvent);
    return { scenario, events };
  });
}

// ---------------------------------------------------------------------------
// Replay helper
// ---------------------------------------------------------------------------

interface ReplayResult {
  snapshots: Array<{ items: TimelineItem[]; eventIndex: number; eventType: string }>;
  messageCompleteIndex: number | null;
  turnId: string | null;
  messageId: string | null;
  activeSessionId: string | null;
  activeSessionLineage: Set<string>;
  hadActiveTurn: boolean;
}

/**
 * Dispatch a single WS event through the same code paths the real page uses.
 *
 * This is the router-faithful dispatch that mirrors production routing so the
 * golden replay can catch bugs in paths that ChatTimeline.applyEvent does not
 * handle (e.g. conversation_runtime_snapshot → applyRuntimeSnapshot).
 *
 * Routing rules (mirrors +page.svelte handleSocketEvent):
 * - conversation_runtime_snapshot → chatTimeline.applyRuntimeSnapshot()
 *   (the real page calls applyConversationRuntimeSnapshot which delegates here)
 * - All other events → chatTimeline.applyEvent()
 */
function dispatchEvent(ct: ChatTimeline, event: CognisWebSocketEvent): void {
  if (event.type === 'conversation_runtime_snapshot') {
    const hasActiveTurn = event.has_active_turn ?? (
      event.active_streams.length > 0
      || event.active_tool_outputs.length > 0
      || event.active_thinking.length > 0
    );
    // No session filtering in the replay (activeSessionId = null → all pass)
    const items = event.timeline_items ?? [];
    ct.applyRuntimeSnapshot(items, hasActiveTurn);
    return;
  }
  // Synthetic event emitted by the e2e capture to model a history/view refresh
  // (reloadConversationSubloads → replaceAll). The real page calls
  // chatTimeline.replaceAll(projectedTimelineItems(view)); the golden replay
  // mirrors that here so INV-REFRESH-NO-DROP exercises the real path.
  if ((event as { type: string }).type === 'conversation_view_refresh') {
    const items = (event as unknown as { timeline_items?: unknown[] }).timeline_items ?? [];
    ct.replaceAll(items as never);
    return;
  }
  ct.applyEvent(event);
}

function replayEvents(events: CognisWebSocketEvent[]): ReplayResult {
  const ct = new ChatTimeline();
  const snapshots: ReplayResult['snapshots'] = [];
  let messageCompleteIndex: number | null = null;
  let turnId: string | null = null;
  let messageId: string | null = null;

  // Track session state to exercise the session filter (the blind spot that
  // let sub-session tool_calls render in the main timeline).
  let activeSessionId: string | null = null;
  let turnInProgress = false;
  // For the golden replay we use a simple lineage: just the active session itself.
  // A real compaction predecessor would be in the lineage too, but for the
  // sub-session leak test the active session is the only lineage member.
  const activeSessionLineage = new Set<string>();
  let hadActiveTurn = false;

  for (let i = 0; i < events.length; i++) {
    const event = events[i]!;

    // Track activeSessionId from state snapshots / reconnected events.
    if (event.type === 'conversation_state_snapshot' || event.type === 'conversation_state_delta') {
      const state = (event as any).state ?? (event as any).replace?.state;
      const sid = state?.offsets?.active_session_id ?? state?.active_session?.session_id;
      if (typeof sid === 'string') {
        activeSessionId = sid;
        activeSessionLineage.clear();
        activeSessionLineage.add(sid);
      }
      const hasActiveTurn = state?.active_turn?.has_active_turn;
      if (typeof hasActiveTurn === 'boolean') {
        turnInProgress = hasActiveTurn;
        if (hasActiveTurn) hadActiveTurn = true;
      }
    }
    if (event.type === 'reconnected') {
      const sid = (event as any).session_id;
      if (typeof sid === 'string') {
        activeSessionId = sid;
        activeSessionLineage.clear();
        activeSessionLineage.add(sid);
      }
      const hat = (event as any).has_active_turn;
      if (typeof hat === 'boolean') turnInProgress = hat;
    }
    if (event.type === 'message_complete') {
      turnInProgress = false;
    }
    if (event.type === 'turn_started') {
      turnInProgress = true;
      hadActiveTurn = true;
    }

    // Route through the same dispatch logic as the real page so the replay
    // catches bugs in paths that applyEvent doesn't handle.
    // Pass activeSessionId + turnInProgress + lineage so the session filter
    // is actually exercised (previously these were null/false → filter bypassed).
    if (event.type === 'conversation_runtime_snapshot') {
      const hasActiveTurn = (event as any).has_active_turn ?? (
        ((event as any).active_streams?.length > 0)
        || ((event as any).active_tool_outputs?.length > 0)
        || ((event as any).active_thinking?.length > 0)
      );
      const items = (event as any).timeline_items ?? [];
      ct.applyRuntimeSnapshot(items, hasActiveTurn);
    } else if ((event as { type: string }).type === 'conversation_view_refresh') {
      const items = (event as unknown as { timeline_items?: unknown[] }).timeline_items ?? [];
      ct.replaceAll(items as never);
    } else {
      ct.applyEvent(event, activeSessionId, turnInProgress, activeSessionLineage);
    }

    // Flush pending rAF patches synchronously for testing
    ct.flushPending();

    const items = ct.toArray();
    snapshots.push({ items, eventIndex: i, eventType: event.type });

    if (event.type === 'message_complete') {
      messageCompleteIndex = i;
      turnId = (event as any).turn_id ?? null;
      messageId = (event as any).message_id ?? null;
    }
  }

  return { snapshots, messageCompleteIndex, turnId, messageId, activeSessionId, activeSessionLineage, hadActiveTurn };
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

const goldenFiles = loadGoldenFiles();

if (goldenFiles.length === 0) {
  describe('chat-timeline golden replay', () => {
    it.skip('no golden files found — run: uv run pytest tests/e2e/ -m e2e', () => {});
  });
} else {
  describe('chat-timeline golden replay', () => {
    for (const { scenario, events } of goldenFiles) {
      it(`scenario: ${scenario} — ${events.length} events`, () => {
        const { snapshots, messageCompleteIndex, turnId, messageId, activeSessionId, activeSessionLineage, hadActiveTurn } = replayEvents(events);

        const violations = checkAllInvariants(
          snapshots,
          messageCompleteIndex,
          turnId,
          messageId,
          events as Array<{ type: string; has_active_turn?: boolean }>,
        );

        // INV-NO-FOREIGN-SESSION: no sub-session item should survive in the store.
        // Only checked when we have an active session and there was an active turn
        // (the condition under which the turnInProgress bypass previously admitted
        // foreign-session items).
        if (activeSessionId && hadActiveTurn) {
          for (const snapshot of snapshots) {
            violations.push(...checkNoForeignSession(
              snapshot.items,
              snapshot.eventIndex,
              activeSessionId,
              activeSessionLineage,
            ));
          }
        }

        if (violations.length > 0) {
          const report = violations
            .map((v) => `  [${v.invariant}] event=${v.eventIndex} item=${v.itemId ?? 'n/a'}: ${v.message}`)
            .join('\n');
          expect.fail(
            `Invariant violations in scenario ${JSON.stringify(scenario)}:\n${report}`,
          );
        }

        // Basic sanity: at least one timeline-producing event was present.
        // Newer golden captures use Chat v2 state/runtime frames instead of
        // legacy timeline_patch events.
        const timelineInputCount = events.filter((e) => {
          const type = (e as { type: string }).type;
          return type === 'timeline_patch'
          || type === 'chat_v2_frame'
          || type === 'conversation_runtime_snapshot'
          || type === 'conversation_view_refresh'
          || type === 'conversation_state_snapshot'
          || type === 'conversation_state_delta';
        }
        ).length;
        expect(timelineInputCount).toBeGreaterThan(0);

        // Final state should have items
        const finalSnapshot = snapshots[snapshots.length - 1];
        expect(finalSnapshot).toBeDefined();
      });
    }
  });
}
