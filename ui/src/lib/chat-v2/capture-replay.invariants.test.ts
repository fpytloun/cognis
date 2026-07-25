import { describe, expect, it } from 'vitest';
import { readdirSync, readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import {
  applyRealtimeFrame,
  applySnapshot,
  applySyncResponse,
  emptyChatV2State,
  visibleTimelineItems
} from './sync-engine';
import type { ChatRealtimeFrame, ChatSnapshot, ChatSyncResponse, MessageTimelineItem } from './types';

type CaptureRecord =
  | { type: 'snapshot'; payload: ChatSnapshot }
  | { type: 'sync'; payload: ChatSyncResponse }
  | { type: 'frame' | 'chat_v2_frame'; payload: ChatRealtimeFrame }
  | {
      type: 'reconnect';
      payload: { scope: ChatSnapshot['scope']; cursor: string };
    };

function capturePaths(): string[] {
  return readdirSync(resolve(__dirname, 'captures'))
    .filter((name) => name.endsWith('.jsonl'))
    .sort()
    .map((name) => resolve(__dirname, 'captures', name));
}

function readCapture(path: string): CaptureRecord[] {
  return readFileSync(path, 'utf8')
    .trim()
    .split('\n')
    .map((line) => {
      const raw = JSON.parse(line) as Record<string, unknown>;
      const { type, ...payload } = raw;
      expect(['snapshot', 'sync', 'frame', 'chat_v2_frame', 'reconnect'], `${path}: record kind`).toContain(type);
      return { type, payload } as unknown as CaptureRecord;
    });
}

describe('canonical Chat v2 JSONL capture replay', () => {
  it('replays every promoted capture through the production engine', () => {
    const paths = capturePaths();
    expect(paths.length, 'promoted capture directory must not be empty').toBeGreaterThan(0);

    for (const path of paths) {
      replayCapture(path);
    }
  });
});

function replayCapture(path: string): void {
  let state = emptyChatV2State();
  const orderByStep: string[][] = [];
  const records = readCapture(path);
  expect(records.length, path).toBeGreaterThan(0);
  const scopes = new Set<string>();
  const seenKinds = new Set<string>();
  const seenStatuses = new Set<string>();
  let reconnectIndex = -1;
  let lastCursor: string | null = null;

  for (const [index, record] of records.entries()) {
    const payload = record.payload;
    const scope = payload.scope;
    const expectedScopeKey = scope?.kind === 'conversation'
      ? `conversation:${scope.conversation_id}`
      : scope?.kind === 'session'
        ? `session:${scope.session_id}`
        : `task_step:${scope?.step_run_id}`;
    expect(scope?.key, `${path}:${index}: scope`).toEqual(expectedScopeKey);
    scopes.add(scope?.key ?? '');
    if (record.type === 'reconnect') {
      expect(record.payload.cursor, `${path}:${index}: reconnect cursor`).toBe(lastCursor);
      reconnectIndex = index;
    } else if (record.type === 'snapshot') {
      state = applySnapshot(record.payload, state);
      lastCursor = record.payload.cursor;
      for (const item of record.payload.timeline.items) {
        seenKinds.add(item.kind);
        if (item.status) seenStatuses.add(item.status);
      }
    } else if (record.type === 'sync') {
      const sync = record.payload;
      const result = applySyncResponse(state, sync);
      expect(['applied', 'duplicate', 'reset_required'], `${path}: ${result.outcome}`).toContain(result.outcome);
      if (lastCursor && sync.cursor_before !== lastCursor && !sync.reset_required) {
        expect(sync.cursor_before, `${path}:${index}: cursor continuity`).toBe(lastCursor);
      }
      lastCursor = sync.cursor_after;
      state = result.state;
      for (const op of sync.ops) if (op.op === 'upsert_item') {
        seenKinds.add(op.item.kind);
        if (op.item.status) seenStatuses.add(op.item.status);
      }
    } else {
      const frame = record.payload;
      expect(frame.cursor_before, `${path}:${index}: frame cursor`).toBe(lastCursor);
      const result = applyRealtimeFrame(state, frame);
      expect(result.outcome).not.toBe('cursor_mismatch');
      lastCursor = frame.cursor_after;
      state = result.state;
      for (const op of frame.ops) if (op.op === 'upsert_item') {
        seenKinds.add(op.item.kind);
        if (op.item.status) seenStatuses.add(op.item.status);
      }
    }
    const visible = visibleTimelineItems(state);
    orderByStep.push(visible.map((item) => item.id));
    expect(new Set(visible.map((item) => item.id)).size).toBe(visible.length);
    expect(visible.map((item) => item.sort_key)).toEqual(
      [...visible].sort((a, b) => a.sort_key.localeCompare(b.sort_key)).map((item) => item.sort_key)
    );
    for (const item of visible) {
      expect(item.id).toBeTruthy();
      expect(item.kind).toBeTruthy();
      expect(Array.isArray(item.source_refs)).toBe(true);
    }
  }

  expect(scopes.size, path).toBe(1);
  expect(state.syncStatus).toBe('ready');
  expect(orderByStep.at(-1)?.length).toBeGreaterThan(0);
  expect([...seenKinds], path).toEqual(expect.arrayContaining(['message', 'tool_call']));
  expect(seenStatuses, path).toContain('complete');
  const assistant = state.timelineItems.find(
    (item): item is MessageTimelineItem => item.kind === 'message' && item.role === 'assistant'
  );
  expect(assistant?.content.trim(), `${path}: assistant/completion`).toBeTruthy();
  const tool = state.timelineItems.find((item) => item.kind === 'tool_call');
  expect(tool?.status, `${path}: tool result`).toBe('complete');
  expect(tool?.result_preview, `${path}: tool result`).toBeTruthy();
  expect(visibleTimelineItems(state).some((item) => item.status === 'running')).toBe(false);
  const resetIndices = records.flatMap((record, index) => (
    record.type === 'sync' && record.payload.reset_required ? [index] : []
  ));
  if (path.includes('promoted-live-')) {
    expect(resetIndices.length, `${path}: reset sequence`).toBeGreaterThan(0);
    expect(reconnectIndex, `${path}: reconnect sequence`).toBeGreaterThan(0);
  }
  for (const resetAt of resetIndices) {
    expect(resetAt, `${path}: reset index`).toBeGreaterThan(-1);
    expect(records[resetAt + 1]?.type, `${path}:${resetAt}: recovery ordering`).toBe('snapshot');
    const preReset = [...records.slice(0, resetAt)]
      .reverse()
      .find((record) => record.type === 'snapshot');
    const recovery = records[resetAt + 1];
    expect(preReset?.type, `${path}:${resetAt}: pre-reset snapshot`).toBe('snapshot');
    if (preReset?.type !== 'snapshot' || recovery?.type !== 'snapshot') continue;
    const reset = records[resetAt];
    if (reset.type !== 'sync') continue;
    expect(
      [recovery.payload.schema_version, recovery.payload.projection_version],
      `${path}:${resetAt}: recovery generation`
    ).toEqual([reset.payload.schema_version, reset.payload.projection_version]);
    expect(
      [preReset.payload.schema_version, preReset.payload.projection_version],
      `${path}:${resetAt}: generation changed`
    ).not.toEqual([reset.payload.schema_version, reset.payload.projection_version]);
  }
}
