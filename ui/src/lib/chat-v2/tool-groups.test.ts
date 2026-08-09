import { describe, expect, it } from 'vitest';

import { isInternalToolCall, prepareTimelineRows } from './tool-groups';
import { DEFAULT_USER_PREFERENCES } from '$lib/user-preferences';
import type { ActivitySegmentRow } from './tool-groups';
import type { MessageTimelineItem, ThinkingTimelineItem, TimelineItem, ToolCallTimelineItem } from './types';

function sourceRef(seq: number) {
  return {
    store: 'intaris',
    session_id: 'sess-1',
    seq,
    event_type: 'test'
  };
}

function activityEntryIds(row: ActivitySegmentRow): string[] {
  return row.entries.map((entry) => entry.kind === 'assistant'
    ? `assistant:${entry.item.id}`
    : `tools:${entry.group.items.map((item) => item.id).join(',')}`
  );
}

function rowShape(rows: ReturnType<typeof prepareTimelineRows>): string[] {
  return rows.map((row) => {
    if (row.kind === 'activity_segment') {
      return [
        row.kind,
        row.id,
        row.summary.label,
        ...activityEntryIds(row)
      ].join('|');
    }
    if (row.kind === 'tool_group') {
      return [row.kind, row.id, row.summary.label, row.items.map((item) => item.call_id).join(',')].join('|');
    }
    if (row.kind === 'thinking_group') {
      return [row.kind, row.id].join('|');
    }
    return [row.kind, row.item.id].join('|');
  });
}

function tool(id: string, toolName: string, overrides: Partial<ToolCallTimelineItem> = {}): ToolCallTimelineItem {
  return {
    id,
    kind: 'tool_call',
    sort_key: id,
    source_refs: [],
    stable: true,
    status: 'complete',
    call_id: id,
    tool_name: toolName,
    turn_id: 'turn-1',
    turn_cycle_index: 0,
    assistant_phase_index: 0,
    arguments: null,
    is_error: false,
    attachments: [],
    file_diffs: [],
    truncated: false,
    has_full_output: false,
    ...overrides
  };
}

function message(id: string, overrides: Partial<MessageTimelineItem> = {}): MessageTimelineItem {
  return {
    id,
    kind: 'message',
    sort_key: id,
    source_refs: [],
    stable: true,
    role: 'assistant',
    content: 'done',
    message_id: id,
    attachments: [],
    partial: false,
    ...overrides
  };
}

function thinking(id: string, overrides: Partial<ThinkingTimelineItem> = {}): ThinkingTimelineItem {
  return {
    id,
    kind: 'thinking',
    sort_key: id,
    source_refs: [],
    stable: true,
    turn_id: 'turn-1',
    assistant_phase_index: 0,
    blocks: [
      {
        id: `${id}-block`,
        content: 'reasoning',
        status: 'complete',
        duration_ms: 1000
      }
    ],
    ...overrides
  };
}

const THINKING_VISIBLE_PREFERENCES = {
  ...DEFAULT_USER_PREFERENCES,
  chat: {
    ...DEFAULT_USER_PREFERENCES.chat,
    show_thinking_blocks: true
  }
};

const SEPARATE_ASSISTANT_MESSAGES_PREFERENCES = {
  ...DEFAULT_USER_PREFERENCES,
  chat: {
    ...DEFAULT_USER_PREFERENCES.chat,
    keep_assistant_messages_separate: true
  }
};

describe('prepareTimelineRows', () => {
  it('groups consecutive same-turn same-phase tool calls', () => {
    const rows = prepareTimelineRows([
      tool('a', 'read', { duration_ms: 10 }),
      tool('b', 'grep', { duration_ms: 20 })
    ], DEFAULT_USER_PREFERENCES);

    expect(rows).toHaveLength(1);
    expect(rows[0].kind).toBe('tool_group');
    if (rows[0].kind !== 'tool_group') return;
    expect(rows[0].summary.label).toBe('Exploring…');
    expect(rows[0].summary.toolCount).toBe(2);
    expect(rows[0].summary.detailLabel).toBe('2 tools');
    expect(rows[0].summary.durationMs).toBe(30);
  });

  it('upgrades contiguous file reads to editing activity when edits are present', () => {
    const rows = prepareTimelineRows([
      tool('a', 'read'),
      tool('b', 'grep'),
      tool('c', 'apply_patch', {
        file_diffs: [{ path: 'src/app.ts', diff: '--- a/src/app.ts\n+++ b/src/app.ts\n-old\n+new\n+extra' }]
      }),
      tool('d', 'read'),
      tool('e', 'grep')
    ], DEFAULT_USER_PREFERENCES);

    expect(rows).toHaveLength(1);
    expect(rows[0].kind).toBe('tool_group');
    if (rows[0].kind !== 'tool_group') return;
    expect(rows[0].summary.label).toBe('Editing files…');
    expect(rows[0].summary.toolCount).toBe(5);
    expect(rows[0].summary.detailLabel).toBe('1 file (+2/-1)');
    expect(rows[0].items.map((item) => item.id)).toEqual(['a', 'b', 'c', 'd', 'e']);
  });

  it('keeps read-only file activity labeled as exploration', () => {
    const rows = prepareTimelineRows([
      tool('a', 'read'),
      tool('b', 'grep'),
      tool('c', 'glob'),
      tool('d', 'list_directory')
    ], DEFAULT_USER_PREFERENCES);

    expect(rows).toHaveLength(1);
    expect(rows[0].kind).toBe('tool_group');
    if (rows[0].kind !== 'tool_group') return;
    expect(rows[0].summary.label).toBe('Exploring…');
    expect(rows[0].summary.toolCount).toBe(4);
  });

  it('keeps file-read and file-edit classifications split while merging same edit runs across cycles', () => {
    const rows = prepareTimelineRows([
      tool('a', 'read', { assistant_phase_index: 0, turn_cycle_index: 0 }),
      tool('b', 'grep', { assistant_phase_index: 1, turn_cycle_index: 0 }),
      tool('c', 'apply_patch', {
        assistant_phase_index: 2,
        turn_cycle_index: 1,
        file_diffs: [{ path: 'src/app.ts', diff: '--- a/src/app.ts\n+++ b/src/app.ts\n-old\n+new' }]
      }),
      tool('d', 'read', { assistant_phase_index: 3, turn_cycle_index: 1 }),
      tool('e', 'apply_patch', {
        assistant_phase_index: 4,
        turn_cycle_index: 2,
        file_diffs: [{ path: 'src/other.ts', diff: '--- a/src/other.ts\n+++ b/src/other.ts\n-old\n+new\n+more' }]
      })
    ], DEFAULT_USER_PREFERENCES);

    expect(rows.map((row) => row.kind)).toEqual(['tool_group', 'activity_segment']);
    expect(rows[0].kind === 'tool_group' ? rows[0].summary.label : '').toBe('Exploring…');
    expect(rows[0].kind === 'tool_group' ? rows[0].items.map((item) => item.id) : []).toEqual(['a', 'b']);
    expect(rows[1].kind === 'activity_segment' ? rows[1].summary.label : '').toBe('Editing files…');
    expect(rows[1].kind === 'activity_segment' ? activityEntryIds(rows[1]) : []).toEqual(['tools:c,d', 'tools:e']);
  });

  it('does not merge non-file exploration into editing activity', () => {
    const rows = prepareTimelineRows([
      tool('a', 'artifact_read'),
      tool('b', 'apply_patch')
    ], DEFAULT_USER_PREFERENCES);

    expect(rows.map((row) => row.kind)).toEqual(['tool_group', 'tool_group']);
    expect(rows[0].kind === 'tool_group' ? rows[0].summary.label : '').toBe('Exploring…');
    expect(rows[1].kind === 'tool_group' ? rows[1].summary.label : '').toBe('Editing files…');
  });

  it('does not merge mixed file and non-file exploration into editing activity', () => {
    const rows = prepareTimelineRows([
      tool('a', 'read'),
      tool('b', 'artifact_read'),
      tool('c', 'apply_patch')
    ], DEFAULT_USER_PREFERENCES);

    expect(rows.map((row) => row.kind)).toEqual(['tool_group', 'tool_group']);
    expect(rows[0].kind === 'tool_group' ? rows[0].summary.label : '').toBe('Exploring…');
    expect(rows[0].kind === 'tool_group' ? rows[0].items.map((item) => item.id) : []).toEqual(['a', 'b']);
    expect(rows[1].kind === 'tool_group' ? rows[1].summary.label : '').toBe('Editing files…');
  });

  it('preserves chronological order across interleaved messages', () => {
    const rows = prepareTimelineRows([
      tool('a', 'read'),
      message('m'),
      tool('b', 'grep')
    ], DEFAULT_USER_PREFERENCES);

    expect(rows.map((row) => row.kind)).toEqual(['tool_group', 'item', 'tool_group']);
    expect(rows[0].kind).toBe('tool_group');
    expect(rows[1].kind).toBe('item');
    expect(rows[2].kind).toBe('tool_group');
    if (rows[0].kind !== 'tool_group' || rows[1].kind !== 'item' || rows[2].kind !== 'tool_group') return;
    expect(rows[0].items.map((item) => item.id)).toEqual(['a']);
    expect(rows[1].item.id).toBe('m');
    expect(rows[2].items.map((item) => item.id)).toEqual(['b']);
  });

  it('wraps an assistant preface with immediately following same-cycle tool groups', () => {
    const rows = prepareTimelineRows([
      message('m', { turn_id: 'turn-1', turn_cycle_index: 0, content: 'I will inspect this.' }),
      tool('a', 'read', { turn_cycle_index: 0 }),
      tool('b', 'grep', { turn_cycle_index: 0 })
    ], DEFAULT_USER_PREFERENCES);

    expect(rows).toHaveLength(1);
    expect(rows[0].kind).toBe('activity_segment');
    if (rows[0].kind !== 'activity_segment') return;
    expect(rows[0].summary.label).toBe('Exploring…');
    expect(rows[0].summary.toolCount).toBe(2);
    expect(activityEntryIds(rows[0])).toEqual(['assistant:m', 'tools:a,b']);
    expect(rows[0].toolGroups[0].items.map((item) => item.id)).toEqual(['a', 'b']);
  });

  it('keeps live assistant text standalone until backend confirms tool activity', () => {
    const rows = prepareTimelineRows([
      message('m', {
        turn_id: 'turn-1',
        turn_cycle_index: 0,
        content: 'I will inspect this.',
        partial: true,
        status: 'running'
      }),
      tool('a', 'read', { turn_cycle_index: 0, status: 'running', stable: false })
    ], DEFAULT_USER_PREFERENCES);

    expect(rows).toHaveLength(2);
    expect(rows[0].kind).toBe('item');
    expect(rows[1].kind).toBe('tool_group');
    if (rows[0].kind !== 'item' || rows[1].kind !== 'tool_group') return;
    expect(rows[0].item.id).toBe('m');
    expect(rows[1].items.map((item) => item.id)).toEqual(['a']);
  });

  it('keeps live assistant text EXPANDED during streaming even when backend marks tool activity', () => {
    // The streamed text must stay visible through the fold transition (no
    // mid-stream collapse into the one-line preview). The segment collapses
    // only once the assistant message completes.
    const rows = prepareTimelineRows([
      message('m', {
        turn_id: 'turn-1',
        turn_cycle_index: 0,
        content: 'I will inspect this.',
        partial: true,
        status: 'running'
      }),
      tool('a', 'read', { turn_cycle_index: 0, status: 'running', stable: false })
    ], DEFAULT_USER_PREFERENCES, [
      {
        turn_id: 'turn-1',
        turn_cycle_index: 0,
        lifecycle_status: 'open',
        has_tool_activity: true
      }
    ]);

    expect(rows).toHaveLength(1);
    expect(rows[0].kind).toBe('activity_segment');
    if (rows[0].kind !== 'activity_segment') return;
    expect(rows[0].id).toBe('activity-segment:turn-1:0:t:a');
    expect(rows[0].defaultExpanded).toBe(true);
    expect(activityEntryIds(rows[0])).toEqual(['assistant:m', 'tools:a']);
  });

  it('collapses the segment once the live assistant text completes', () => {
    const rows = prepareTimelineRows([
      message('m', {
        turn_id: 'turn-1',
        turn_cycle_index: 0,
        content: 'I inspected this.',
        partial: false,
        status: 'complete'
      }),
      tool('a', 'read', { turn_cycle_index: 0, status: 'complete', stable: true })
    ], DEFAULT_USER_PREFERENCES, [
      {
        turn_id: 'turn-1',
        turn_cycle_index: 0,
        lifecycle_status: 'complete',
        has_tool_activity: true
      }
    ]);

    expect(rows[0].kind).toBe('activity_segment');
    if (rows[0].kind !== 'activity_segment') return;
    expect(rows[0].defaultExpanded).toBe(false);
  });

  it('keeps activity segment ids stable when assistant completion folds with existing tool activity', () => {
    const runningRows = prepareTimelineRows([
      message('m', {
        turn_id: 'turn-1',
        turn_cycle_index: 0,
        content: 'I will inspect this.',
        partial: true,
        status: 'running'
      }),
      tool('a', 'read', { turn_cycle_index: 0, status: 'running', stable: false })
    ], DEFAULT_USER_PREFERENCES, [
      {
        turn_id: 'turn-1',
        turn_cycle_index: 0,
        lifecycle_status: 'open',
        has_tool_activity: true
      }
    ]);
    const completedRows = prepareTimelineRows([
      message('m', {
        turn_id: 'turn-1',
        turn_cycle_index: 0,
        content: 'I will inspect this.',
        partial: false,
        status: 'complete'
      }),
      tool('a', 'read', { turn_cycle_index: 0, status: 'complete', stable: true })
    ], DEFAULT_USER_PREFERENCES, [
      {
        turn_id: 'turn-1',
        turn_cycle_index: 0,
        lifecycle_status: 'complete',
        has_tool_activity: true
      }
    ]);

    expect(runningRows[0].kind).toBe('activity_segment');
    expect(completedRows[0].kind).toBe('activity_segment');
    if (runningRows[0].kind !== 'activity_segment' || completedRows[0].kind !== 'activity_segment') return;
    expect(runningRows[0].id).toBe('activity-segment:turn-1:0:t:a');
    expect(completedRows[0].id).toBe(runningRows[0].id);
  });

  it('keeps the segment id stable when a tool result arrives with file_diffs (reclassify edit)', () => {
    // A bash/read group whose result later carries file_diffs reclassifies
    // command/explore -> edit. With a stable (turn, cycle) id, that must NOT
    // change the segment id (which would remount the block and reset expansion).
    const before = prepareTimelineRows([
      message('m', { turn_id: 'turn-1', turn_cycle_index: 0, content: 'Editing.' }),
      tool('a', 'bash', { turn_cycle_index: 0, status: 'running', stable: false })
    ], DEFAULT_USER_PREFERENCES);
    const after = prepareTimelineRows([
      message('m', { turn_id: 'turn-1', turn_cycle_index: 0, content: 'Editing.' }),
      tool('a', 'bash', {
        turn_cycle_index: 0,
        status: 'complete',
        stable: true,
        file_diffs: [{ path: 'src/app.ts', diff: '--- a/src/app.ts\n+++ b/src/app.ts\n-old\n+new' }]
      })
    ], DEFAULT_USER_PREFERENCES);

    expect(before[0].kind).toBe('activity_segment');
    expect(after[0].kind).toBe('activity_segment');
    if (before[0].kind !== 'activity_segment' || after[0].kind !== 'activity_segment') return;
    expect(before[0].id).toBe('activity-segment:turn-1:0:t:a');
    expect(after[0].id).toBe(before[0].id);
  });

  it('keeps the segment id stable when an assistant folds at the front of a tool-first segment', () => {
    // A tool-first segment (>1 file-work group) that later gains an assistant
    // message at its front must keep its id (first-item id would otherwise flip
    // from the tool to the assistant and remount the block, resetting state).
    const toolFirst = prepareTimelineRows([
      tool('a', 'apply_patch', {
        turn_cycle_index: 0,
        file_diffs: [{ path: 'a.ts', diff: '--- a/a.ts\n+++ b/a.ts\n-x\n+y' }]
      }),
      message('sep', { turn_id: 'turn-1', turn_cycle_index: 0, content: '', role: 'assistant' }),
      tool('b', 'read', { turn_cycle_index: 0 })
    ], DEFAULT_USER_PREFERENCES);
    const toolFirstSegment = toolFirst.find((row) => row.kind === 'activity_segment');
    // The segment id is derived from the stable (turn, cycle), so regardless of
    // whether the assistant entry is first, the id is the same.
    expect(toolFirstSegment?.kind === 'activity_segment' ? toolFirstSegment.id : null).toBe(
      'activity-segment:turn-1:0:t:a'
    );
  });

  it('keeps all activity segment ids unique across a complex same-turn timeline', () => {
    // Guard against keyed-each id collisions: segments derive their id from the
    // stable (turn, cycle) plus a per-cycle ordinal, so no two segments — even
    // ones that share a (turn, cycle) after a classification split — can ever
    // produce the same key.
    const rows = prepareTimelineRows([
      message('m1', { turn_id: 'turn-1', turn_cycle_index: 0, content: 'Explore.' }),
      tool('a', 'read', { turn_cycle_index: 0 }),
      tool('b', 'grep', { turn_cycle_index: 0 }),
      message('m2', { turn_id: 'turn-1', turn_cycle_index: 1, content: 'Edit.' }),
      tool('c', 'apply_patch', {
        turn_cycle_index: 1,
        file_diffs: [{ path: 'x.ts', diff: '--- a/x.ts\n+++ b/x.ts\n-o\n+n' }]
      }),
      message('m3', { turn_id: 'turn-1', turn_cycle_index: 2, content: 'Run.' }),
      tool('d', 'bash', { turn_cycle_index: 2 })
    ], DEFAULT_USER_PREFERENCES);

    const segmentIds = rows.flatMap((row) => (row.kind === 'activity_segment' ? [row.id] : []));
    expect(segmentIds.length).toBeGreaterThanOrEqual(1);
    expect(new Set(segmentIds).size).toBe(segmentIds.length);
  });

  it('groups same-cycle tools with distinct per-tool phases under one assistant', () => {
    // Phases are assigned per tool call (each call bumps the counter), so
    // same-cycle tools carrying phases 0 and 1 are ONE logical batch — the
    // grouping must match what the live overlay produced for the same calls.
    const rows = prepareTimelineRows([
      message('m', { turn_id: 'turn-1', turn_cycle_index: 0, content: 'I will inspect this.' }),
      tool('a', 'read', { turn_cycle_index: 0, assistant_phase_index: 0 }),
      tool('b', 'grep', { turn_cycle_index: 0, assistant_phase_index: 1 })
    ], DEFAULT_USER_PREFERENCES);

    expect(rows).toHaveLength(1);
    expect(rows[0].kind).toBe('activity_segment');
    if (rows[0].kind !== 'activity_segment') return;
    expect(activityEntryIds(rows[0])).toEqual(['assistant:m', 'tools:a,b']);
    expect(rows[0].summary.toolCount).toBe(2);
  });

  it('keeps adjacent same-kind assistant tool activity in one stable segment across stamped cycles', () => {
    const rows = prepareTimelineRows([
      message('m1', { turn_id: 'turn-1', turn_cycle_index: 0, content: 'I will edit this.' }),
      tool('a', 'apply_patch', { turn_cycle_index: 0 }),
      message('m2', { turn_id: 'turn-1', turn_cycle_index: 1, content: 'I need one more edit.' }),
      tool('b', 'apply_patch', { turn_cycle_index: 1 })
    ], DEFAULT_USER_PREFERENCES);

    expect(rows).toHaveLength(1);
    expect(rows[0].kind).toBe('activity_segment');
    if (rows[0].kind !== 'activity_segment') return;
    expect(activityEntryIds(rows[0])).toEqual(['assistant:m1', 'tools:a', 'assistant:m2', 'tools:b']);
    expect(rows[0].summary.toolCount).toBe(2);
  });

  it('uses the latest assistant snippet while an activity group is running', () => {
    const rows = prepareTimelineRows([
      message('m1', { turn_id: 'turn-1', turn_cycle_index: 0, content: 'First snippet.' }),
      tool('a', 'read', { turn_cycle_index: 0 }),
      message('m2', {
        turn_id: 'turn-1',
        turn_cycle_index: 1,
        content: 'Latest running snippet.',
        partial: true,
        status: 'running'
      }),
      tool('b', 'grep', { turn_cycle_index: 1, status: 'running', stable: false })
    ], DEFAULT_USER_PREFERENCES, [
      { turn_id: 'turn-1', turn_cycle_index: 0, lifecycle_status: 'complete', has_tool_activity: true },
      { turn_id: 'turn-1', turn_cycle_index: 1, lifecycle_status: 'open', has_tool_activity: true }
    ]);

    expect(rows).toHaveLength(1);
    expect(rows[0].kind).toBe('activity_segment');
    if (rows[0].kind !== 'activity_segment') return;
    expect(rows[0].summary.status).toBe('running');
    expect(rows[0].assistantPreview).toBe('Latest running snippet.');
  });

  it('uses the first assistant snippet after an activity group completes', () => {
    const rows = prepareTimelineRows([
      message('m1', { turn_id: 'turn-1', turn_cycle_index: 0, content: 'First snippet.' }),
      tool('a', 'read', { turn_cycle_index: 0, status: 'complete' }),
      message('m2', { turn_id: 'turn-1', turn_cycle_index: 1, content: 'Later snippet.' }),
      tool('b', 'grep', { turn_cycle_index: 1, status: 'complete' })
    ], DEFAULT_USER_PREFERENCES, [
      { turn_id: 'turn-1', turn_cycle_index: 0, lifecycle_status: 'complete', has_tool_activity: true },
      { turn_id: 'turn-1', turn_cycle_index: 1, lifecycle_status: 'complete', has_tool_activity: true }
    ]);

    expect(rows).toHaveLength(1);
    expect(rows[0].kind).toBe('activity_segment');
    if (rows[0].kind !== 'activity_segment') return;
    expect(rows[0].summary.status).toBe('complete');
    expect(rows[0].assistantPreview).toBe('First snippet.');
  });

  it('keeps a leading same-kind tool group with following stamped cycles', () => {
    const rows = prepareTimelineRows([
      tool('a', 'bash', { source_refs: [sourceRef(1)], turn_cycle_index: 0 }),
      message('m1', {
        turn_id: 'turn-1',
        turn_cycle_index: 1,
        content: 'I will check the PR head.',
        source_refs: [sourceRef(2)]
      }),
      tool('b', 'bash', { source_refs: [sourceRef(3)], turn_cycle_index: 1 }),
      message('m2', {
        turn_id: 'turn-1',
        turn_cycle_index: 2,
        content: 'I will resolve the thread.',
        source_refs: [sourceRef(4)]
      }),
      tool('c', 'bash', { source_refs: [sourceRef(5)], turn_cycle_index: 2 })
    ], DEFAULT_USER_PREFERENCES);

    expect(rows.map((row) => row.kind)).toEqual(['activity_segment']);
    expect(rows[0].kind === 'activity_segment' ? activityEntryIds(rows[0]) : []).toEqual([
      'tools:a',
      'assistant:m1',
      'tools:b',
      'assistant:m2',
      'tools:c'
    ]);
  });

  it('keeps same-kind tool groups in one segment when the assistant is in a later stamped cycle', () => {
    const rows = prepareTimelineRows([
      tool('a', 'read', { turn_cycle_index: 0 }),
      message('m1', {
        turn_id: 'turn-1',
        turn_cycle_index: 1,
        content: 'I will inspect the next file.'
      }),
      tool('b', 'grep', { turn_cycle_index: 1 })
    ], DEFAULT_USER_PREFERENCES);

    expect(rows.map((row) => row.kind)).toEqual(['activity_segment']);
    expect(rows[0].kind === 'activity_segment' ? activityEntryIds(rows[0]) : []).toEqual([
      'tools:a',
      'assistant:m1',
      'tools:b'
    ]);
  });

  it('folds a same-cycle post-tool assistant message into the activity group', () => {
    const rows = prepareTimelineRows([
      tool('a', 'delegate', { source_refs: [sourceRef(1)], turn_cycle_index: 0 }),
      message('m1', {
        turn_id: 'turn-1',
        turn_cycle_index: 0,
        content: 'Delegation finished.',
        source_refs: [sourceRef(2)]
      })
    ], DEFAULT_USER_PREFERENCES);

    expect(rows).toHaveLength(1);
    expect(rows[0].kind).toBe('activity_segment');
    if (rows[0].kind !== 'activity_segment') return;
    expect(activityEntryIds(rows[0])).toEqual(['tools:a', 'assistant:m1']);
  });

  it('does not trailing-fold a LIVE (partial) assistant even with a matching-cycle tool group', () => {
    // Regression: while streaming, the final answer must render standalone.
    // The backend cycle can transiently collide with the tool group's cycle
    // (phase-vs-cycle skew, completion-frame coercion). If a live assistant
    // folded here it would vanish into the collapsed activity segment and
    // re-appear only when a later frame corrected its cycle.
    const rows = prepareTimelineRows([
      tool('a', 'read', { turn_cycle_index: 0, status: 'complete', stable: true }),
      message('m1', {
        turn_id: 'turn-1',
        turn_cycle_index: 0,
        content: 'Streaming the final answer…',
        partial: true,
        stable: false,
        status: 'running'
      })
    ], DEFAULT_USER_PREFERENCES, [
      { turn_id: 'turn-1', turn_cycle_index: 0, lifecycle_status: 'open', has_tool_activity: true }
    ]);

    // The tool renders as its own group; the live assistant stays a standalone
    // item and is NOT absorbed into the segment.
    expect(rows.map((row) => row.kind)).toEqual(['tool_group', 'item']);
  });

  it('trailing-folds the SAME assistant once it settles (partial -> complete)', () => {
    const rows = prepareTimelineRows([
      tool('a', 'read', { turn_cycle_index: 0, status: 'complete', stable: true }),
      message('m1', {
        turn_id: 'turn-1',
        turn_cycle_index: 0,
        content: 'The final answer.',
        partial: false,
        stable: true,
        status: 'complete'
      })
    ], DEFAULT_USER_PREFERENCES, [
      { turn_id: 'turn-1', turn_cycle_index: 0, lifecycle_status: 'complete', has_tool_activity: true }
    ]);

    expect(rows).toHaveLength(1);
    expect(rows[0].kind).toBe('activity_segment');
    if (rows[0].kind !== 'activity_segment') return;
    expect(activityEntryIds(rows[0])).toEqual(['tools:a', 'assistant:m1']);
  });

  it('does not fold a missing-cycle assistant with the immediately following same-turn tool group after reload', () => {
    const rows = prepareTimelineRows([
      message('m1', {
        turn_id: 'turn-1',
        assistant_phase_index: 99,
        content: 'I will inspect this.',
        source_refs: [sourceRef(1)]
      }),
      tool('a', 'read', {
        assistant_phase_index: 2,
        turn_cycle_index: 4,
        source_refs: [sourceRef(2)]
      })
    ], DEFAULT_USER_PREFERENCES);

    expect(rows.map((row) => row.kind)).toEqual(['item', 'tool_group']);
  });

  it('requires assistant cycle metadata regardless of unrelated backend cycle state', () => {
    const rows = prepareTimelineRows([
      message('m1', {
        turn_id: 'turn-1',
        content: 'I will inspect this.',
        source_refs: [sourceRef(1)]
      }),
      tool('a', 'read', {
        turn_cycle_index: 2,
        status: 'complete',
        source_refs: [sourceRef(2)]
      })
    ], DEFAULT_USER_PREFERENCES, [
      {
        turn_id: 'turn-2',
        turn_cycle_index: 0,
        lifecycle_status: 'complete',
        has_tool_activity: true
      }
    ]);

    expect(rows.map((row) => row.kind)).toEqual(['item', 'tool_group']);
  });

  it('does not infer assistant cycle metadata from backend tool cycle state', () => {
    const rows = prepareTimelineRows([
      message('m1', {
        turn_id: 'turn-1',
        content: 'I will inspect this.',
        source_refs: [sourceRef(1)]
      }),
      tool('a', 'read', {
        turn_cycle_index: 2,
        status: 'complete',
        source_refs: [sourceRef(2)]
      })
    ], DEFAULT_USER_PREFERENCES, [
      {
        turn_id: 'turn-1',
        turn_cycle_index: 2,
        lifecycle_status: 'complete',
        has_tool_activity: true
      }
    ]);

    expect(rows.map((row) => row.kind)).toEqual(['item', 'tool_group']);
  });

  it('keeps a missing-cycle assistant separate while preserving following tool-cycle groups', () => {
    const rows = prepareTimelineRows([
      message('m1', {
        turn_id: 'turn-1',
        assistant_phase_index: 2,
        content: 'I will inspect this.',
        source_refs: [sourceRef(1)]
      }),
      tool('a', 'read', {
        assistant_phase_index: 2,
        turn_cycle_index: 4,
        source_refs: [sourceRef(2)]
      }),
      tool('b', 'grep', {
        assistant_phase_index: 2,
        turn_cycle_index: 5,
        source_refs: [sourceRef(3)]
      })
    ], DEFAULT_USER_PREFERENCES);

    expect(rows.map((row) => row.kind)).toEqual(['item', 'activity_segment']);
    expect(rows[1].kind === 'activity_segment' ? activityEntryIds(rows[1]) : []).toEqual(['tools:a', 'tools:b']);
  });

  it('does not fold a streaming assistant preface when its cycle has not resolved yet', () => {
    const rows = prepareTimelineRows([
      message('m1', {
        turn_id: 'turn-1',
        content: 'I will inspect this.',
        partial: true,
        status: 'running',
        stable: false,
        source_refs: [sourceRef(1)]
      }),
      tool('a', 'read', {
        turn_cycle_index: 4,
        status: 'running',
        stable: false,
        source_refs: [sourceRef(2)]
      })
    ], DEFAULT_USER_PREFERENCES);

    expect(rows).toHaveLength(2);
    expect(rows[0].kind).toBe('item');
    expect(rows[1].kind).toBe('tool_group');
    if (rows[0].kind !== 'item' || rows[1].kind !== 'tool_group') return;
    expect(rows[0].item.id).toBe('m1');
    expect(rows[1].items.map((item) => item.id)).toEqual(['a']);
  });

  it('does not fold a missing-cycle final assistant message backward without following activity', () => {
    const rows = prepareTimelineRows([
      tool('a', 'read', { turn_cycle_index: 4 }),
      message('m1', {
        turn_id: 'turn-1',
        content: 'Final answer.',
        source_refs: [sourceRef(2)]
      })
    ], DEFAULT_USER_PREFERENCES);

    expect(rows.map((row) => row.kind)).toEqual(['tool_group', 'item']);
    expect(rows[0].kind === 'tool_group' ? rows[0].items.map((item) => item.id) : []).toEqual(['a']);
    expect(rows[1].kind === 'item' ? rows[1].item.id : '').toBe('m1');
  });

  it('keeps a later-cycle same-kind tool inside the earlier assistant activity run', () => {
    const rows = prepareTimelineRows([
      message('m1', { turn_id: 'turn-1', turn_cycle_index: 0, content: 'I will inspect this.' }),
      tool('a', 'read', { turn_cycle_index: 0 }),
      tool('b', 'grep', { turn_cycle_index: 1 })
    ], DEFAULT_USER_PREFERENCES);

    expect(rows).toHaveLength(1);
    expect(rows[0].kind).toBe('activity_segment');
    if (rows[0].kind !== 'activity_segment') return;
    expect(activityEntryIds(rows[0])).toEqual(['assistant:m1', 'tools:a', 'tools:b']);
  });

  it('keeps assistant-only cycles standalone between tool cycles', () => {
    const rows = prepareTimelineRows([
      tool('a', 'bash', { turn_cycle_index: 0 }),
      message('m1', { turn_id: 'turn-1', turn_cycle_index: 1, content: 'Standalone update.' }),
      tool('b', 'bash', { turn_cycle_index: 2 })
    ], DEFAULT_USER_PREFERENCES);

    expect(rows.map((row) => row.kind)).toEqual(['tool_group', 'item', 'tool_group']);
    expect(rows[0].kind === 'tool_group' ? rows[0].items.map((item) => item.id) : []).toEqual(['a']);
    expect(rows[1].kind === 'item' ? rows[1].item.id : '').toBe('m1');
    expect(rows[2].kind === 'tool_group' ? rows[2].items.map((item) => item.id) : []).toEqual(['b']);
  });

  it('keeps adjacent same-kind command activity in one segment across stamped cycles', () => {
    const rows = prepareTimelineRows([
      tool('a', 'bash', { turn_cycle_index: 0 }),
      tool('b', 'bash', { turn_cycle_index: 1 })
    ], DEFAULT_USER_PREFERENCES);

    expect(rows.map((row) => row.kind)).toEqual(['activity_segment']);
    expect(rows[0].kind === 'activity_segment' ? activityEntryIds(rows[0]) : []).toEqual(['tools:a', 'tools:b']);
  });

  it('keeps adjacent same-turn exploration activity in one segment across stamped cycles', () => {
    const rows = prepareTimelineRows([
      tool('a', 'read', { turn_id: 'turn-1', turn_cycle_index: 0 }),
      tool('b', 'grep', { turn_id: 'turn-1', turn_cycle_index: 1 })
    ], DEFAULT_USER_PREFERENCES);

    expect(rows.map((row) => row.kind)).toEqual(['activity_segment']);
    expect(rows[0].kind === 'activity_segment' ? rows[0].summary.label : '').toBe('Exploring…');
    expect(rows[0].kind === 'activity_segment' ? rows[0].summary.toolCount : 0).toBe(2);
    expect(rows[0].kind === 'activity_segment' ? activityEntryIds(rows[0]) : []).toEqual(['tools:a', 'tools:b']);
  });

  it('escalates file work to editing when an edit appears, keeping pre-edit reads exploring', () => {
    // Forward escalation (no cycle metadata): reads before the first edit stay
    // "Exploring…"; the edit begins a new "Editing files…" segment and the
    // following reads fold into it. Pre-edit reads are never folded backward.
    const rows = prepareTimelineRows([
      tool('a', 'read', { turn_cycle_index: undefined as unknown as number }),
      tool('b', 'grep', { turn_cycle_index: undefined as unknown as number }),
      tool('c', 'apply_patch', { turn_cycle_index: undefined as unknown as number }),
      tool('d', 'read', { turn_cycle_index: undefined as unknown as number }),
      tool('e', 'grep', { turn_cycle_index: undefined as unknown as number })
    ], DEFAULT_USER_PREFERENCES);

    expect(rows.map((row) => row.kind)).toEqual(['activity_segment', 'activity_segment']);
    expect(rows[0].kind === 'activity_segment' ? rows[0].summary.label : '').toBe('Exploring…');
    expect(rows[0].kind === 'activity_segment' ? activityEntryIds(rows[0]) : []).toEqual([
      'tools:a', 'tools:b'
    ]);
    expect(rows[1].kind === 'activity_segment' ? rows[1].summary.label : '').toBe('Editing files…');
    expect(rows[1].kind === 'activity_segment' ? activityEntryIds(rows[1]) : []).toEqual([
      'tools:c', 'tools:d', 'tools:e'
    ]);
    const segmentIds = rows.flatMap((row) => row.kind === 'activity_segment' ? [row.id] : []);
    expect(new Set(segmentIds).size).toBe(segmentIds.length);
  });

  it('keeps same-kind activity with matching assistant prefaces in one segment across stamped cycles', () => {
    const rows = prepareTimelineRows([
      message('m1', { turn_id: 'turn-1', turn_cycle_index: 0, content: 'I will inspect this.' }),
      tool('a', 'read', { turn_cycle_index: 0 }),
      message('m2', { turn_id: 'turn-1', turn_cycle_index: 1, content: 'I will inspect more.' }),
      tool('b', 'grep', { turn_cycle_index: 1 })
    ], DEFAULT_USER_PREFERENCES);

    expect(rows).toHaveLength(1);
    expect(rows[0].kind === 'activity_segment' ? activityEntryIds(rows[0]) : []).toEqual([
      'assistant:m1',
      'tools:a',
      'assistant:m2',
      'tools:b'
    ]);
  });

  it('keeps assistant prefaces separate while preserving tool groups', () => {
    const rows = prepareTimelineRows([
      message('m1', { turn_id: 'turn-1', turn_cycle_index: 0, content: 'I will inspect this.' }),
      tool('a', 'read', { turn_cycle_index: 0 }),
      tool('b', 'grep', { turn_cycle_index: 0 }),
      message('m2', { turn_id: 'turn-1', turn_cycle_index: 1, content: 'I will inspect more.' }),
      tool('c', 'glob', { turn_cycle_index: 1 })
    ], SEPARATE_ASSISTANT_MESSAGES_PREFERENCES);

    expect(rows.map((row) => row.kind)).toEqual(['item', 'tool_group', 'item', 'tool_group']);
    expect(rows[0].kind === 'item' ? rows[0].item.id : '').toBe('m1');
    expect(rows[1].kind === 'tool_group' ? rows[1].items.map((item) => item.id) : []).toEqual(['a', 'b']);
    expect(rows[2].kind === 'item' ? rows[2].item.id : '').toBe('m2');
    expect(rows[3].kind === 'tool_group' ? rows[3].items.map((item) => item.id) : []).toEqual(['c']);
  });

  it('merges adjacent same-kind tool groups across cycles while assistant messages stay separate', () => {
    const rows = prepareTimelineRows([
      message('m1', { turn_id: 'turn-1', turn_cycle_index: 0, content: 'I will inspect this.' }),
      tool('a', 'read', { turn_cycle_index: 0, assistant_phase_index: 0 }),
      tool('b', 'grep', { turn_cycle_index: 1, assistant_phase_index: 1 }),
      tool('c', 'glob', { turn_cycle_index: 2, assistant_phase_index: 2 })
    ], SEPARATE_ASSISTANT_MESSAGES_PREFERENCES);

    expect(rows.map((row) => row.kind)).toEqual(['item', 'tool_group']);
    expect(rows[0].kind === 'item' ? rows[0].item.id : '').toBe('m1');
    expect(rows[1].kind === 'tool_group' ? rows[1].items.map((item) => item.id) : []).toEqual(['a', 'b', 'c']);
    expect(rows[1].kind === 'tool_group' ? rows[1].summary.toolCount : 0).toBe(3);
  });

  it('does not merge same-kind tool groups from different turns while assistant messages stay separate', () => {
    const rows = prepareTimelineRows([
      tool('a', 'read', { turn_id: 'turn-1', turn_cycle_index: 0 }),
      tool('b', 'grep', { turn_id: 'turn-2', turn_cycle_index: 0 })
    ], SEPARATE_ASSISTANT_MESSAGES_PREFERENCES);

    expect(rows.map((row) => row.kind)).toEqual(['tool_group', 'tool_group']);
    expect(rows[0].kind === 'tool_group' ? rows[0].items.map((item) => item.id) : []).toEqual(['a']);
    expect(rows[1].kind === 'tool_group' ? rows[1].items.map((item) => item.id) : []).toEqual(['b']);
  });

  it('keeps streaming assistant messages separate from same-cycle tool activity', () => {
    const rows = prepareTimelineRows([
      message('m1', {
        turn_id: 'turn-1',
        turn_cycle_index: 0,
        stable: false,
        partial: true,
        content: 'Inspecting…'
      }),
      tool('a', 'read', {
        turn_cycle_index: 0,
        stable: false,
        status: 'running'
      })
    ], SEPARATE_ASSISTANT_MESSAGES_PREFERENCES);

    expect(rows.map((row) => row.kind)).toEqual(['item', 'tool_group']);
    expect(rows[0].kind === 'item' ? rows[0].item.id : '').toBe('m1');
    expect(rows[1].kind === 'tool_group' ? rows[1].items.map((item) => item.id) : []).toEqual(['a']);
  });

  it('keeps completed assistant output separate after grouped tool activity', () => {
    const rows = prepareTimelineRows([
      tool('a', 'read', { turn_cycle_index: 0 }),
      tool('b', 'grep', { turn_cycle_index: 0 }),
      message('final', { turn_id: 'turn-1', content: 'Done.' })
    ], SEPARATE_ASSISTANT_MESSAGES_PREFERENCES);

    expect(rows.map((row) => row.kind)).toEqual(['tool_group', 'item']);
    expect(rows[0].kind === 'tool_group' ? rows[0].items.map((item) => item.id) : []).toEqual(['a', 'b']);
    expect(rows[1].kind === 'item' ? rows[1].item.id : '').toBe('final');
  });

  it('keeps delegation tool activity grouped while assistant messages stay separate', () => {
    const rows = prepareTimelineRows([
      message('preface', { turn_id: 'turn-1', turn_cycle_index: 0, content: 'Delegating this.' }),
      tool('delegate', 'delegate', { turn_cycle_index: 0 }),
      tool('follow-up', 'follow_up_subsession', { turn_cycle_index: 0 })
    ], SEPARATE_ASSISTANT_MESSAGES_PREFERENCES);

    expect(rows.map((row) => row.kind)).toEqual(['item', 'tool_group']);
    expect(rows[0].kind === 'item' ? rows[0].item.id : '').toBe('preface');
    expect(rows[1].kind === 'tool_group' ? rows[1].summary.label : '').toBe('Delegating work…');
    expect(rows[1].kind === 'tool_group' ? rows[1].items.map((item) => item.id) : []).toEqual([
      'delegate',
      'follow-up'
    ]);
  });

  it('streaming and reload produce identical file-work escalation', () => {
    // The escalation must be identical whether the items arrive as a live
    // stream or are reprojected from canonical history — join/continuation keys
    // are name-derived and immutable, never dependent on live status.
    const items = (stable: boolean) => [
      tool('r1', 'read', { turn_cycle_index: 0, stable, status: stable ? 'complete' : 'running' as const }),
      tool('e1', 'apply_patch', { turn_cycle_index: 1, stable, status: stable ? 'complete' : 'running' as const }),
      tool('r2', 'read', { turn_cycle_index: 2, stable, status: stable ? 'complete' : 'running' as const })
    ];
    const live = prepareTimelineRows(items(false), DEFAULT_USER_PREFERENCES);
    const reloaded = prepareTimelineRows(items(true), DEFAULT_USER_PREFERENCES);

    const summarize = (rows: ReturnType<typeof prepareTimelineRows>) =>
      rows.map((row) => {
        if (row.kind === 'activity_segment') return `SEG:${row.summary.label}:${activityEntryIds(row).join(',')}`;
        if (row.kind === 'tool_group') return `TG:${row.summary.label}`;
        if (row.kind === 'item') return `IT:${row.item.id}`;
        return `OTHER:${row.kind}`;
      });

    expect(summarize(live)).toEqual(summarize(reloaded));
    // r1 explores (lone read group, no assistant → bare tool group); e1
    // escalates to editing; r2 folds into the editing run.
    expect(summarize(reloaded)).toEqual([
      'TG:Exploring…',
      'SEG:Editing files…:tools:e1,tools:r2'
    ]);
  });

  it('a bash with late file_diffs still escapes the editing run (name-based classification)', () => {
    // file_diffs is live-mutable and can appear on a bash RESULT. It must not
    // reclassify bash as file-edit, otherwise streaming and reload would group
    // differently and the command would wrongly absorb the following read.
    const rows = prepareTimelineRows([
      tool('e1', 'apply_patch', {
        turn_cycle_index: 0,
        file_diffs: [{ path: 'a.ts', diff: '--- a/a.ts\n+++ b/a.ts\n-x\n+y' }]
      }),
      tool('sh', 'bash', {
        turn_cycle_index: 1,
        // A shell that happens to report file diffs on completion.
        file_diffs: [{ path: 'b.ts', diff: '--- a/b.ts\n+++ b/b.ts\n-p\n+q' }]
      }),
      tool('r1', 'read', { turn_cycle_index: 2 })
    ], DEFAULT_USER_PREFERENCES);

    // e1 (lone edit group, no assistant) renders as a bare tool group; bash
    // breaks any file-work run; the read after bash starts its own exploring.
    const kinds = rows.map((row) =>
      row.kind === 'activity_segment' ? `SEG:${row.summary.label}` : row.kind === 'tool_group' ? `TG:${row.summary.label}` : `IT`
    );
    expect(kinds).toEqual(['TG:Editing files…', 'TG:Running commands…', 'TG:Exploring…']);
  });

  it('folds file reads after an edit into the same Editing segment', () => {
    // Reads that follow an edit are part of the edit flow (read-modify-write),
    // so they collapse into the "Editing files…" segment rather than starting a
    // new "Exploring…" one.
    const rows = prepareTimelineRows([
      message('m1', { turn_id: 'turn-1', turn_cycle_index: 0, content: 'I will edit this.' }),
      tool('a', 'apply_patch', { turn_cycle_index: 0 }),
      message('m2', { turn_id: 'turn-1', turn_cycle_index: 1, content: 'I will inspect now.' }),
      tool('b', 'read', { turn_cycle_index: 1 })
    ], DEFAULT_USER_PREFERENCES);

    expect(rows.map((row) => row.kind)).toEqual(['activity_segment']);
    expect(rows[0].kind === 'activity_segment' ? rows[0].summary.label : '').toBe('Editing files…');
    expect(rows[0].kind === 'activity_segment' ? activityEntryIds(rows[0]) : []).toEqual([
      'assistant:m1',
      'tools:a',
      'assistant:m2',
      'tools:b'
    ]);
  });

  it('keeps failed edit recovery message with the following same-turn file work after reload', () => {
    const rows = prepareTimelineRows([
      tool('failed-edit', 'apply_patch', {
        turn_cycle_index: 2,
        status: 'failed',
        is_error: true
      }),
      message('recovery', {
        turn_id: 'turn-1',
        turn_cycle_index: 3,
        content: 'The patch failed; I will read the exact worktree files.'
      }),
      tool('read-1', 'read', { turn_cycle_index: 3 }),
      tool('read-2', 'read', { turn_cycle_index: 3 }),
      tool('fixed-edit', 'apply_patch', {
        turn_cycle_index: 3,
        file_diffs: [{ path: 'src/app.ts', diff: '--- a/src/app.ts\n+++ b/src/app.ts\n-old\n+new' }]
      })
    ], DEFAULT_USER_PREFERENCES);

    expect(rows).toHaveLength(1);
    expect(rows[0].kind).toBe('activity_segment');
    if (rows[0].kind !== 'activity_segment') return;
    expect(rows[0].summary.label).toBe('Editing files…');
    expect(activityEntryIds(rows[0])).toEqual([
      'tools:failed-edit',
      'assistant:recovery',
      'tools:read-1,read-2,fixed-edit'
    ]);
  });

  it('escalates a contiguous file flow: pre-edit reads explore, then one editing run', () => {
    // The reported behavior: a few read-only cycles show "Exploring…"; once an
    // apply_patch appears the run becomes "Editing files…" and subsequent file
    // reads collapse into it; a non-file tool (bash) escapes the editing run.
    const rows = prepareTimelineRows([
      message('m0', { turn_id: 'turn-1', turn_cycle_index: 0, content: 'I understand the request.' }),
      message('m1', { turn_id: 'turn-1', turn_cycle_index: 1, content: 'I will inspect the files.' }),
      tool('read-1', 'read', { turn_cycle_index: 1 }),
      tool('edit-1', 'apply_patch', { turn_cycle_index: 2 }),
      message('m3', { turn_id: 'turn-1', turn_cycle_index: 3, content: 'Patch failed; I will inspect context.' }),
      tool('read-2', 'read', { turn_cycle_index: 3 }),
      tool('edit-2', 'apply_patch', { turn_cycle_index: 4 }),
      message('m5', { turn_id: 'turn-1', turn_cycle_index: 5, content: 'I will run tests.' }),
      tool('test-1', 'bash', { turn_cycle_index: 5 }),
      message('m6', { turn_id: 'turn-1', turn_cycle_index: 6, content: 'Done.' })
    ], DEFAULT_USER_PREFERENCES);

    expect(rows.map((row) => row.kind)).toEqual([
      'item',
      'activity_segment',
      'activity_segment',
      'activity_segment',
      'item'
    ]);
    expect(rows[0].kind === 'item' ? rows[0].item.id : '').toBe('m0');
    // Pre-edit read stays Exploring (never folded backward into Editing).
    expect(rows[1].kind === 'activity_segment' ? rows[1].summary.label : '').toBe('Exploring…');
    expect(rows[1].kind === 'activity_segment' ? activityEntryIds(rows[1]) : []).toEqual([
      'assistant:m1',
      'tools:read-1'
    ]);
    // Edit starts Editing; the following read (read-2) and edit-2 fold in.
    expect(rows[2].kind === 'activity_segment' ? rows[2].summary.label : '').toBe('Editing files…');
    expect(rows[2].kind === 'activity_segment' ? activityEntryIds(rows[2]) : []).toEqual([
      'tools:edit-1',
      'assistant:m3',
      'tools:read-2',
      'tools:edit-2'
    ]);
    // bash escapes the editing run into its own command segment.
    expect(rows[3].kind === 'activity_segment' ? rows[3].summary.label : '').toBe('Running commands…');
    expect(rows[3].kind === 'activity_segment' ? activityEntryIds(rows[3]) : []).toEqual([
      'assistant:m5',
      'tools:test-1'
    ]);
    expect(rows[4].kind === 'item' ? rows[4].item.id : '').toBe('m6');
  });

  it('groups adjacent same-turn tools despite live assistant phase instability', () => {
    const rows = prepareTimelineRows([
      tool('a', 'custom_tool', { assistant_phase_index: 0, stable: false, status: 'running' }),
      tool('b', 'another_custom_tool', { assistant_phase_index: 1, stable: false, status: 'running' }),
      tool('c', 'third_custom_tool', { assistant_phase_index: 2, stable: false, status: 'running' })
    ], DEFAULT_USER_PREFERENCES);

    expect(rows).toHaveLength(1);
    expect(rows[0].kind).toBe('tool_group');
    if (rows[0].kind !== 'tool_group') return;
    expect(rows[0].summary.label).toBe('Using tools…');
    expect(rows[0].summary.toolCount).toBe(3);
    expect(rows[0].items.map((item) => item.id)).toEqual(['a', 'b', 'c']);
  });

  it('groups completed same-turn tools identically to their live projection', () => {
    // Live and settled grouping must be structurally identical for the same
    // logical items: joining is keyed on turn + cycle + tool classification,
    // never on per-tool phases or live-mutable status predicates.
    const live = prepareTimelineRows([
      tool('a', 'custom_tool', { assistant_phase_index: 0, stable: false, status: 'running' }),
      tool('b', 'another_custom_tool', { assistant_phase_index: 1, stable: false, status: 'running' })
    ], DEFAULT_USER_PREFERENCES);
    const settled = prepareTimelineRows([
      tool('a', 'custom_tool', { assistant_phase_index: 0, stable: true, status: 'complete' }),
      tool('b', 'another_custom_tool', { assistant_phase_index: 1, stable: true, status: 'complete' })
    ], DEFAULT_USER_PREFERENCES);

    expect(settled.map((row) => row.kind)).toEqual(live.map((row) => row.kind));
    expect(settled).toHaveLength(1);
    expect(settled[0].kind).toBe('tool_group');
    if (settled[0].kind !== 'tool_group' || live[0].kind !== 'tool_group') return;
    expect(settled[0].items.map((item) => item.id)).toEqual(live[0].items.map((item) => item.id));
  });

  it('keeps streaming and reload activity grouping equivalent for stamped turn cycles', () => {
    const live = prepareTimelineRows([
      message('message:turn-1:phase:0', {
        message_id: 'turn-1',
        turn_id: 'turn-1',
        turn_cycle_index: 0,
        partial: true,
        stable: false,
        status: 'running',
        content: 'I will inspect this.'
      }),
      tool('tool:call-read', 'read', {
        call_id: 'call-read',
        id: 'tool:call-read',
        turn_cycle_index: 0,
        stable: false,
        status: 'running'
      }),
      message('message:turn-1:phase:1', {
        message_id: 'turn-1',
        turn_id: 'turn-1',
        turn_cycle_index: 1,
        partial: true,
        stable: false,
        status: 'running',
        content: 'I will run tests.'
      }),
      tool('tool:call-test', 'bash', {
        call_id: 'call-test',
        id: 'tool:call-test',
        turn_cycle_index: 1,
        stable: false,
        status: 'running'
      })
    ], DEFAULT_USER_PREFERENCES, [
      { turn_id: 'turn-1', turn_cycle_index: 0, lifecycle_status: 'open', has_tool_activity: true },
      { turn_id: 'turn-1', turn_cycle_index: 1, lifecycle_status: 'open', has_tool_activity: true }
    ]);
    const reloaded = prepareTimelineRows([
      message('message:turn-1:phase:0', {
        message_id: 'turn-1',
        turn_id: 'turn-1',
        turn_cycle_index: 0,
        partial: false,
        stable: true,
        status: 'complete',
        content: 'I will inspect this.'
      }),
      tool('tool:call-read', 'read', {
        call_id: 'call-read',
        id: 'tool:call-read',
        turn_cycle_index: 0,
        stable: true,
        status: 'complete'
      }),
      message('message:turn-1:phase:1', {
        message_id: 'turn-1',
        turn_id: 'turn-1',
        turn_cycle_index: 1,
        partial: false,
        stable: true,
        status: 'complete',
        content: 'I will run tests.'
      }),
      tool('tool:call-test', 'bash', {
        call_id: 'call-test',
        id: 'tool:call-test',
        turn_cycle_index: 1,
        stable: true,
        status: 'complete'
      })
    ], DEFAULT_USER_PREFERENCES, [
      { turn_id: 'turn-1', turn_cycle_index: 0, lifecycle_status: 'complete', has_tool_activity: true },
      { turn_id: 'turn-1', turn_cycle_index: 1, lifecycle_status: 'complete', has_tool_activity: true }
    ]);

    expect(rowShape(live)).toEqual(rowShape(reloaded));
    expect(rowShape(live)).toEqual([
      'activity_segment|activity-segment:turn-1:0:t:call-read|Exploring…|assistant:message:turn-1:phase:0|tools:tool:call-read',
      'activity_segment|activity-segment:turn-1:1:t:call-test|Running commands…|assistant:message:turn-1:phase:1|tools:tool:call-test'
    ]);
  });

  it('keeps generic same-kind tools in one segment across different turn cycles', () => {
    const rows = prepareTimelineRows([
      tool('a', 'custom_tool', { turn_cycle_index: 0, stable: true, status: 'complete' }),
      tool('b', 'another_custom_tool', { turn_cycle_index: 1, stable: true, status: 'complete' })
    ], DEFAULT_USER_PREFERENCES);

    expect(rows.map((row) => row.kind)).toEqual(['activity_segment']);
    expect(rows[0].kind === 'activity_segment' ? rows[0].summary.label : '').toBe('Using tools…');
    expect(rows[0].kind === 'activity_segment' ? activityEntryIds(rows[0]) : []).toEqual(['tools:a', 'tools:b']);
  });

  it('deduplicates repeated live and persisted tool-call projections by call id', () => {
    const rows = prepareTimelineRows([
      tool('runtime-a', 'custom_tool', { call_id: 'call-a', status: 'running' }),
      tool('persisted-a', 'custom_tool', { call_id: 'call-a', status: 'complete' }),
      tool('runtime-b', 'another_custom_tool', { call_id: 'call-b', status: 'running' })
    ], DEFAULT_USER_PREFERENCES);

    expect(rows).toHaveLength(1);
    expect(rows[0].kind).toBe('tool_group');
    if (rows[0].kind !== 'tool_group') return;
    expect(rows[0].summary.toolCount).toBe(2);
    expect(rows[0].items.map((item) => item.call_id)).toEqual(['call-a', 'call-b']);
    expect(rows[0].items[0].id).toBe('persisted-a');
    expect(rows[0].items[0].status).toBe('complete');
  });

  it('keeps failed exploration groups collapsed by default and omits failure detail', () => {
    const rows = prepareTimelineRows([
      tool('a', 'read'),
      tool('b', 'grep', { is_error: true, status: 'failed' })
    ], DEFAULT_USER_PREFERENCES);

    expect(rows[0].kind).toBe('tool_group');
    if (rows[0].kind !== 'tool_group') return;
    expect(rows[0].summary.status).toBe('failed');
    expect(rows[0].summary.detailLabel).toBe('2 tools');
    expect(rows[0].defaultExpanded).toBe(false);
  });

  it('adds failure detail for shell, generic, and web tool groups', () => {
    const shellRows = prepareTimelineRows([
      tool('bash', 'bash', { is_error: true, status: 'failed' })
    ], DEFAULT_USER_PREFERENCES);
    const genericRows = prepareTimelineRows([
      tool('custom', 'custom_tool', { is_error: true, status: 'failed' })
    ], DEFAULT_USER_PREFERENCES);
    const webRows = prepareTimelineRows([
      tool('web', 'web_search', { is_error: true, status: 'failed' })
    ], DEFAULT_USER_PREFERENCES);

    expect(shellRows[0].kind === 'tool_group' ? shellRows[0].summary.detailLabel : '').toBe('1 tool (1 failed)');
    expect(genericRows[0].kind === 'tool_group' ? genericRows[0].summary.detailLabel : '').toBe('1 tool (1 failed)');
    expect(webRows[0].kind === 'tool_group' ? webRows[0].summary.detailLabel : '').toBe('1 tool (1 failed)');
  });

  it('keeps group identity stable when live tools append', () => {
    const firstRows = prepareTimelineRows([
      tool('a', 'read'),
      tool('b', 'grep')
    ], DEFAULT_USER_PREFERENCES);
    const appendedRows = prepareTimelineRows([
      tool('a', 'read'),
      tool('b', 'grep'),
      tool('c', 'glob')
    ], DEFAULT_USER_PREFERENCES);

    expect(firstRows[0].kind).toBe('tool_group');
    expect(appendedRows[0].kind).toBe('tool_group');
    if (firstRows[0].kind !== 'tool_group' || appendedRows[0].kind !== 'tool_group') return;
    expect(appendedRows[0].id).toBe(firstRows[0].id);
    expect(appendedRows[0].summary.toolCount).toBe(3);
  });

  it('renders a single edit tool as an edit group with file diff stats', () => {
    const rows = prepareTimelineRows([
      tool('patch', 'apply_patch', {
        file_diffs: [
          { path: 'src/app.ts', diff: '--- a/src/app.ts\n+++ b/src/app.ts\n-old\n+new\n+extra' },
          { path: 'src/lib.ts', diff: '--- a/src/lib.ts\n+++ b/src/lib.ts\n-old\n+new' }
        ]
      })
    ], DEFAULT_USER_PREFERENCES);

    expect(rows).toHaveLength(1);
    expect(rows[0].kind).toBe('tool_group');
    if (rows[0].kind !== 'tool_group') return;
    expect(rows[0].summary.label).toBe('Editing files…');
    expect(rows[0].summary.detailLabel).toBe('2 files (+3/-2)');
  });

  it('omits zero diff stats from edit group details', () => {
    const rows = prepareTimelineRows([
      tool('patch', 'apply_patch', {
        file_diffs: [
          { path: 'src/app.ts', diff: '--- a/src/app.ts\n+++ b/src/app.ts' }
        ]
      })
    ], DEFAULT_USER_PREFERENCES);

    expect(rows).toHaveLength(1);
    expect(rows[0].kind).toBe('tool_group');
    if (rows[0].kind !== 'tool_group') return;
    expect(rows[0].summary.detailLabel).toBe('1 file');
  });

  it('renders a single visible tool call as a group immediately', () => {
    const rows = prepareTimelineRows([
      tool('read', 'read', { status: 'running' })
    ], DEFAULT_USER_PREFERENCES);

    expect(rows).toHaveLength(1);
    expect(rows[0].kind).toBe('tool_group');
    if (rows[0].kind !== 'tool_group') return;
    expect(rows[0].summary.label).toBe('Exploring…');
    expect(rows[0].summary.detailLabel).toBe('1 tool');
    expect(rows[0].summary.status).toBe('running');
  });

  it('recomputes cached tool group summaries when a running tool completes', () => {
    const runningRows = prepareTimelineRows([
      tool('read', 'read', { status: 'running', updated_at: '2026-07-08T10:00:00Z' })
    ], DEFAULT_USER_PREFERENCES);
    const completedRows = prepareTimelineRows([
      tool('read', 'read', { status: 'complete', updated_at: '2026-07-08T10:00:01Z' })
    ], DEFAULT_USER_PREFERENCES);

    expect(runningRows[0].kind).toBe('tool_group');
    expect(completedRows[0].kind).toBe('tool_group');
    if (runningRows[0].kind !== 'tool_group' || completedRows[0].kind !== 'tool_group') return;
    expect(runningRows[0].summary.status).toBe('running');
    expect(completedRows[0].summary.status).toBe('complete');
  });

  it('recomputes cached activity segment summaries when a running tool completes', () => {
    const runningRows = prepareTimelineRows([
      message('m', { turn_id: 'turn-1', turn_cycle_index: 0, content: 'I will inspect this.' }),
      tool('read', 'read', {
        turn_cycle_index: 0,
        status: 'running',
        updated_at: '2026-07-08T10:00:00Z'
      })
    ], DEFAULT_USER_PREFERENCES);
    const completedRows = prepareTimelineRows([
      message('m', { turn_id: 'turn-1', turn_cycle_index: 0, content: 'I will inspect this.' }),
      tool('read', 'read', {
        turn_cycle_index: 0,
        status: 'complete',
        updated_at: '2026-07-08T10:00:01Z'
      })
    ], DEFAULT_USER_PREFERENCES);

    expect(runningRows[0].kind).toBe('activity_segment');
    expect(completedRows[0].kind).toBe('activity_segment');
    if (runningRows[0].kind !== 'activity_segment' || completedRows[0].kind !== 'activity_segment') return;
    expect(runningRows[0].summary.status).toBe('running');
    expect(completedRows[0].summary.status).toBe('complete');
  });

  it('classifies native read-only inspection tools as exploration', () => {
    const rows = prepareTimelineRows([
      tool('subsession', 'get_subsession'),
      tool('directory', 'list_directory'),
      tool('tools', 'search_tools'),
      tool('tasks', 'list_tasks'),
      tool('workflow', 'get_workflow'),
      tool('office', 'office_read')
    ], {
      ...DEFAULT_USER_PREFERENCES,
      chat: {
        ...DEFAULT_USER_PREFERENCES.chat,
        show_internal_tool_calls: true
      }
    });

    expect(rows).toHaveLength(1);
    expect(rows[0].kind).toBe('tool_group');
    if (rows[0].kind !== 'tool_group') return;
    expect(rows[0].summary.label).toBe('Exploring…');
    expect(rows[0].summary.toolCount).toBe(6);
  });

  it('classifies all memory tools as memory access', () => {
    const rows = prepareTimelineRows([
      tool('search', 'memory_search'),
      tool('add', 'memory_add'),
      tool('artifact', 'memory_save_artifact')
    ], DEFAULT_USER_PREFERENCES);

    expect(rows).toHaveLength(1);
    expect(rows[0].kind).toBe('tool_group');
    if (rows[0].kind !== 'tool_group') return;
    expect(rows[0].summary.label).toBe('Accessing memory…');
    expect(rows[0].summary.toolCount).toBe(3);
  });

  it('hides interactive prompt tools by default and keeps them ungrouped when shown', () => {
    const items = [
      tool('before', 'read'),
      tool('prompt', 'request_user_input', { status: 'waiting' }),
      tool('after', 'grep')
    ];
    const hiddenRows = prepareTimelineRows(items, DEFAULT_USER_PREFERENCES);
    expect(hiddenRows.map((row) => row.kind)).toEqual(['tool_group']);

    const rows = prepareTimelineRows(items, {
      ...DEFAULT_USER_PREFERENCES,
      chat: {
        ...DEFAULT_USER_PREFERENCES.chat,
        show_internal_tool_calls: true
      }
    });

    expect(rows.map((row) => row.kind)).toEqual(['tool_group', 'item', 'tool_group']);
    expect(rows[1].kind).toBe('item');
    if (rows[1].kind !== 'item') return;
    expect(rows[1].item.kind).toBe('tool_call');
    if (rows[1].item.kind !== 'tool_call') return;
    expect(rows[1].item.tool_name).toBe('request_user_input');
  });

  it('classifies task and managed conversation control tools as delegation', () => {
    const rows = prepareTimelineRows([
      tool('task', 'cancel_task'),
      tool('conversation', 'agent_conversation_send'),
      tool('subsession', 'cancel_subsession')
    ], DEFAULT_USER_PREFERENCES);

    expect(rows).toHaveLength(1);
    expect(rows[0].kind).toBe('tool_group');
    if (rows[0].kind !== 'tool_group') return;
    expect(rows[0].summary.label).toBe('Delegating work…');
    expect(rows[0].summary.toolCount).toBe(3);
  });

  it('classifies delegate lineage tools as delegation', () => {
    const rows = prepareTimelineRows([
      tool('delegate', 'delegate'),
      tool('retry', 'retry_subsession'),
      tool('follow-up', 'follow_up_subsession'),
      tool('fork', 'fork_subsession'),
      tool('legacy-fork', 'fork')
    ], DEFAULT_USER_PREFERENCES);

    expect(rows).toHaveLength(1);
    expect(rows[0].kind).toBe('tool_group');
    if (rows[0].kind !== 'tool_group') return;
    expect(rows[0].summary.label).toBe('Delegating work…');
    expect(rows[0].summary.toolCount).toBe(5);
  });

  it('keeps managed conversation inspection tools grouped as delegation work', () => {
    const rows = prepareTimelineRows([
      tool('get', 'agent_conversation_get'),
      tool('list', 'agent_conversation_list'),
      tool('wait', 'agent_conversation_wait')
    ], DEFAULT_USER_PREFERENCES);

    expect(rows).toHaveLength(1);
    expect(rows[0].kind).toBe('tool_group');
    if (rows[0].kind !== 'tool_group') return;
    expect(rows[0].summary.label).toBe('Delegating work…');
  });

  it('starts a new live tool segment after a different tool category', () => {
    const rows = prepareTimelineRows([
      tool('a', 'read'),
      message('m1'),
      tool('b', 'apply_patch'),
      message('m2'),
      tool('c', 'grep')
    ], DEFAULT_USER_PREFERENCES);

    expect(rows.map((row) => row.kind)).toEqual(['tool_group', 'item', 'tool_group', 'item', 'tool_group']);
    expect(rows[0].kind === 'tool_group' ? rows[0].summary.label : '').toBe('Exploring…');
    expect(rows[2].kind === 'tool_group' ? rows[2].summary.label : '').toBe('Editing files…');
    expect(rows[4].kind === 'tool_group' ? rows[4].summary.label : '').toBe('Exploring…');
  });

  it('groups consecutive thinking blocks with aggregate thought count and duration', () => {
    const rows = prepareTimelineRows([
      thinking('think-a', { blocks: [{ id: 'a', content: 'a', status: 'complete', duration_ms: 3000 }] }),
      thinking('think-b', { blocks: [{ id: 'b', content: 'b', status: 'complete', duration_ms: 5000 }] })
    ], THINKING_VISIBLE_PREFERENCES);

    expect(rows).toHaveLength(1);
    expect(rows[0].kind).toBe('thinking_group');
    if (rows[0].kind !== 'thinking_group') return;
    expect(rows[0].summary.label).toBe('Thinking…');
    expect(rows[0].summary.detailLabel).toBe('2 thoughts');
    expect(rows[0].summary.durationMs).toBe(8000);
  });

  it('groups a single thinking item when it contains multiple thinking blocks', () => {
    const rows = prepareTimelineRows([
      thinking('think-a', {
        blocks: [
          { id: 'a', content: 'a', status: 'complete', duration_ms: 3000 },
          { id: 'b', content: 'b', status: 'complete', duration_ms: 5000 }
        ]
      })
    ], THINKING_VISIBLE_PREFERENCES);

    expect(rows).toHaveLength(1);
    expect(rows[0].kind).toBe('thinking_group');
    if (rows[0].kind !== 'thinking_group') return;
    expect(rows[0].summary.detailLabel).toBe('2 thoughts');
    expect(rows[0].summary.durationMs).toBe(8000);
  });

  it('hides thinking blocks by default', () => {
    const rows = prepareTimelineRows([
      thinking('think-default'),
      tool('read', 'read')
    ], DEFAULT_USER_PREFERENCES);

    expect(rows).toHaveLength(1);
    expect(rows[0].kind).toBe('tool_group');
    if (rows[0].kind !== 'tool_group') return;
    expect(rows[0].items[0].id).toBe('read');
  });

  it('hides thinking and internal helper tools based on preferences', () => {
    const rows = prepareTimelineRows([
      {
        id: 'thinking',
        kind: 'thinking',
        sort_key: 'thinking',
        source_refs: [],
        stable: true,
        blocks: []
      },
      tool('todo', 'todo_write'),
      tool('todo-list', 'todo_list'),
      tool('step-todo-list', 'step_todo_list'),
      tool('search-tools', 'search_tools'),
      tool('describe-tool', 'describe_tool'),
      tool('validate-tool-call', 'validate_tool_call'),
      tool('skill-load', 'skill_load'),
      tool('skill-asset', 'skill_asset_materialize'),
      tool('profile', 'switch_agent_profile'),
      tool('executor', 'switch_executor'),
      tool('question', 'request_user_input', { status: 'waiting' }),
      tool('step-question', 'step_request_questions', { status: 'waiting' }),
      tool('read-output', 'read_tool_output'),
      tool('search-output', 'search_tool_output'),
      tool('list-output-anchors', 'list_tool_output_anchors'),
      tool('read-output-anchor', 'read_tool_output_anchor'),
      tool('read', 'read')
    ], DEFAULT_USER_PREFERENCES);

    expect(rows).toHaveLength(1);
    expect(rows[0].kind).toBe('tool_group');
    if (rows[0].kind !== 'tool_group') return;
    expect(rows[0].items[0].id).toBe('read');
  });

  it('restores each newly hidden internal helper when the preference is enabled', () => {
    const newlyInternalTools = [
      'todo_list',
      'step_todo_list',
      'switch_agent_profile',
      'switch_executor',
      'request_user_input',
      'step_request_questions',
      'attach_artifact'
    ];
    const visiblePreferences = {
      ...DEFAULT_USER_PREFERENCES,
      chat: {
        ...DEFAULT_USER_PREFERENCES.chat,
        show_internal_tool_calls: true
      }
    };

    for (const toolName of newlyInternalTools) {
      const item = tool(toolName, toolName);
      expect(isInternalToolCall(item)).toBe(true);
      expect(prepareTimelineRows([item], DEFAULT_USER_PREFERENCES)).toEqual([]);
      expect(prepareTimelineRows([item], visiblePreferences)).toHaveLength(1);
    }
  });

  it('shows failed internal helper tools even when internal helpers are hidden', () => {
    const rows = prepareTimelineRows([
      tool('skill-load', 'skill_load', { is_error: true }),
      tool('skill-asset', 'skill_asset_materialize', { status: 'failed' }),
      tool('todo', 'todo_write'),
      tool('read', 'read')
    ], DEFAULT_USER_PREFERENCES);

    const visibleToolIds = rows.flatMap((row) => {
      if (row.kind === 'tool_group') return row.items.map((item) => item.id);
      if (row.kind === 'activity_segment') {
        return row.toolGroups.flatMap((group) => group.items.map((item) => item.id));
      }
      if (row.kind === 'item' && row.item.kind === 'tool_call') return [row.item.id];
      return [];
    });

    expect(visibleToolIds).toEqual(['skill-load', 'skill-asset', 'read']);
  });

   it('uses dedicated labels for web, browser, image, and knowledgebase groups', () => {
    const webRows = prepareTimelineRows([tool('a', 'web_search'), tool('b', 'web_fetch')], DEFAULT_USER_PREFERENCES);
    const browserRows = prepareTimelineRows([tool('a', 'browser_open'), tool('b', 'browser_snapshot')], DEFAULT_USER_PREFERENCES);
    const imageRows = prepareTimelineRows([tool('a', 'image_generate'), tool('b', 'image_edit')], DEFAULT_USER_PREFERENCES);
    const kbRows = prepareTimelineRows([tool('a', 'knowledgebase_search'), tool('b', 'knowledgebase_read_source_context')], DEFAULT_USER_PREFERENCES);

    expect(webRows[0].kind === 'tool_group' ? webRows[0].summary.label : '').toBe('Searching web…');
    expect(browserRows[0].kind === 'tool_group' ? browserRows[0].summary.label : '').toBe('Using browser…');
    expect(imageRows[0].kind === 'tool_group' ? imageRows[0].summary.label : '').toBe('Generating images…');
    expect(kbRows[0].kind === 'tool_group' ? kbRows[0].summary.label : '').toBe('Querying knowledgebase…');
  });

  it('keeps group row ids stable across the live -> settled phase resolution', () => {
    // While a turn streams, assistant_phase_index is typically null; the turn
    // settle assigns concrete phase numbers. Row ids are keyed-each render
    // keys — if they change at settle, the group block remounts, its expand
    // state resets, and its height change jumps the user's scroll position.
    const liveRows = prepareTimelineRows([
      tool('a', 'read', { assistant_phase_index: null as unknown as number, stable: false, status: 'running' }),
      tool('b', 'grep', { assistant_phase_index: null as unknown as number, stable: false, status: 'pending' })
    ], DEFAULT_USER_PREFERENCES);
    const settledRows = prepareTimelineRows([
      tool('a', 'read', { assistant_phase_index: 2 }),
      tool('b', 'grep', { assistant_phase_index: 2 })
    ], DEFAULT_USER_PREFERENCES);

    expect(liveRows).toHaveLength(1);
    expect(settledRows).toHaveLength(1);
    if (liveRows[0].kind !== 'tool_group' || settledRows[0].kind !== 'tool_group') {
      throw new Error('expected tool_group rows');
    }
    expect(liveRows[0].id).toBe(settledRows[0].id);
  });

  it('keeps thinking group row ids stable across the live -> settled phase resolution', () => {
    const liveRows = prepareTimelineRows([
      thinking('t1', { assistant_phase_index: null as unknown as number }),
      thinking('t2', { assistant_phase_index: null as unknown as number })
    ], THINKING_VISIBLE_PREFERENCES);
    const settledRows = prepareTimelineRows([
      thinking('t1', { assistant_phase_index: 1 }),
      thinking('t2', { assistant_phase_index: 1 })
    ], THINKING_VISIBLE_PREFERENCES);

    expect(liveRows).toHaveLength(1);
    expect(settledRows).toHaveLength(1);
    if (liveRows[0].kind !== 'thinking_group' || settledRows[0].kind !== 'thinking_group') {
      throw new Error('expected thinking_group rows');
    }
    expect(liveRows[0].id).toBe(settledRows[0].id);
  });
});
