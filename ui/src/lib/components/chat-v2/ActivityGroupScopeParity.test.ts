import { cleanup, fireEvent, render, waitFor } from '@testing-library/svelte';
import { afterEach, describe, expect, it } from 'vitest';

import type { ActivitySegmentRow, ToolGroupRow } from '$lib/chat-v2/tool-groups';
import type { MessageTimelineItem, TimelineScope, ToolCallTimelineItem } from '$lib/chat-v2/types';
import ActivitySegmentBlock from './ActivitySegmentBlock.svelte';
import ToolCallGroupBlock from './ToolCallGroupBlock.svelte';
import { activeActivityGroupId } from '$lib/chat-v2/activity-progress';
import type { RuntimeOverlaySnapshot, ThinkingTimelineItem } from '$lib/chat-v2/types';

const scopes: TimelineScope[] = [
  {
    key: 'conversation:conv_1',
    kind: 'conversation',
    conversation_id: 'conv_1',
  },
  {
    key: 'session:sess_1',
    kind: 'session',
    session_id: 'sess_1',
    conversation_id: 'conv_1',
  },
];

function tool(): ToolCallTimelineItem {
  return {
    id: 'tool:call_1',
    kind: 'tool_call',
    sort_key: '0001:0000000001:0000:tool:call_1',
    source_refs: [],
    created_at: '2026-08-25T20:00:00Z',
    status: 'complete',
    stable: true,
    call_id: 'call_1',
    turn_id: 'turn_1',
    tool_name: 'read',
    arguments: { file_path: '/tmp/file.txt' },
    result_preview: 'content',
    is_error: false,
    attachments: [],
    file_diffs: [],
    truncated: false,
    has_full_output: true,
  };
}

function toolGroup(defaultExpanded: boolean): ToolGroupRow {
  return {
    kind: 'tool_group',
    id: 'tool-group:1',
    items: [tool()],
    summary: {
      kind: 'explore',
      label: 'Exploring…',
      icon: 'search',
      accentClass: 'text-sky-300',
      toolCount: 1,
      detailLabel: '1 tool',
      durationMs: 10,
      startedAt: '2026-08-25T20:00:00Z',
      failedCount: 0,
      deniedCount: 0,
      status: 'complete',
    },
    defaultExpanded,
  };
}

function activitySegment(defaultExpanded: boolean): ActivitySegmentRow {
  const group = toolGroup(false);
  return {
    kind: 'activity_segment',
    id: 'activity-segment:1',
    entries: [{ kind: 'tool_group', group }],
    toolGroups: [group],
    summary: group.summary,
    assistantPreview: null,
    defaultExpanded,
  };
}

function expansionButton(
  container: HTMLElement,
  kind: 'tool_group' | 'activity_segment',
): HTMLButtonElement {
  const button = container.querySelector(`[data-kind="${kind}"] > button`);
  if (!(button instanceof HTMLButtonElement)) {
    throw new Error(`Expansion button for ${kind} was not rendered`);
  }
  return button;
}

afterEach(cleanup);

describe('activity group scope parity', () => {
  it('keeps assistant attachments visible after collapse and remount', async () => {
    const row = activitySegment(false);
    const attached: MessageTimelineItem = {
      id: 'assistant:report', message_id: 'turn-report', kind: 'message', role: 'assistant', content: 'Generated report',
      sort_key: '0002', source_refs: [], stable: true, status: 'complete', partial: false,
      attachments: [{
        artifact_id: 'art_report', filename: 'report.csv',
        mime_type: 'text/csv', kind: 'file', size_bytes: 10,
      }],
    };
    row.entries.push({ kind: 'assistant', item: attached });
    const view = render(ActivitySegmentBlock, { row });
    expect(view.getByTestId('collapsed-assistant-attachments')).toBeTruthy();
    expect(view.getByText('report.csv')).toBeTruthy();
    await fireEvent.click(expansionButton(view.container, 'activity_segment'));
    expect(view.queryByTestId('collapsed-assistant-attachments')).toBeNull();
    expect(view.getByText('report.csv')).toBeTruthy();
    view.unmount();
    const restored = render(ActivitySegmentBlock, { row });
    expect(restored.getByTestId('collapsed-assistant-attachments')).toBeTruthy();
    expect(restored.getByText('report.csv')).toBeTruthy();
  });

  it.each([false, true])('uses the same dense expanded spacing as tool groups (compact=%s)', async (compact) => {
    const tools = render(ToolCallGroupBlock, { row: toolGroup(true), compact });
    const segment = render(ActivitySegmentBlock, { row: activitySegment(true), compact });
    await waitFor(() => {
      const toolBody = tools.container.querySelector('[data-kind="tool_group"] > div');
      const segmentBody = segment.container.querySelector('[data-kind="activity_segment"] > div');
      expect(toolBody).not.toBeNull();
      expect(segmentBody).not.toBeNull();
      // Tailwind's sibling-spacing utility is the expanded list's layout contract.
      expect(segmentBody?.className.match(/\bspace-y-\S+/)?.[0])
        .toBe(toolBody?.className.match(/\bspace-y-\S+/)?.[0]);
    });
  });
  const runtime: RuntimeOverlaySnapshot = {
    runtime_epoch: 'epoch_1', runtime_revision: 1, generated_at: '2026-08-25T20:00:00Z',
    has_active_turn: true, active_turn: { turn_id: 'turn_1', session_id: 'sess_1', status: 'running' },
    volatile_items: [],
  };

  it('keeps a completed tool group active through thinking and between cycles', () => {
    const group = toolGroup(false);
    const thinking: ThinkingTimelineItem = {
      id: 'thinking_1', kind: 'thinking', sort_key: '2', source_refs: [],
      stable: false, status: 'running', turn_id: 'turn_1', blocks: [],
    };
    expect(activeActivityGroupId([group], runtime)).toBe(group.id);
    expect(activeActivityGroupId([group, { kind: 'item', item: thinking }], runtime)).toBe(group.id);
    const next = { ...group, id: 'tool-group:2' };
    expect(activeActivityGroupId([group, next], runtime)).toBe(next.id);
  });

  it('stops for waiting, cancellation, a different turn, or a finished runtime', () => {
    const group = toolGroup(false);
    for (const status of ['waiting', 'cancelling'] as const) {
      expect(activeActivityGroupId([group], {
        ...runtime, active_turn: { ...runtime.active_turn!, status },
      })).toBeNull();
    }
    expect(activeActivityGroupId([group], { ...runtime, has_active_turn: false })).toBeNull();
    expect(activeActivityGroupId([group], {
      ...runtime, active_turn: { ...runtime.active_turn!, turn_id: 'turn_2' },
    })).toBeNull();
    expect(activeActivityGroupId([group], null)).toBeNull();
  });

  it.each(['failed', 'denied'] as const)('keeps progress during recovery from a %s tool', (status) => {
    const group = toolGroup(false);
    group.summary.status = status;
    const thinking: ThinkingTimelineItem = {
      id: 'thinking_1', kind: 'thinking', sort_key: '2', source_refs: [],
      stable: false, status: 'running', turn_id: 'turn_1', blocks: [],
    };
    expect(activeActivityGroupId([group, { kind: 'item', item: thinking }], runtime)).toBe(group.id);
    group.items.push({ ...tool(), id: 'tool:call_2', call_id: 'call_2', status: 'running' });
    expect(activeActivityGroupId([group], runtime)).toBe(group.id);
    expect(activeActivityGroupId([group], { ...runtime, has_active_turn: false })).toBeNull();
  });

  it('animates a completed group while its owning activity remains active', async () => {
    const view = render(ToolCallGroupBlock, { row: toolGroup(false), active: true });
    expect(view.container.querySelector('.running-label')).not.toBeNull();
    await view.rerender({ row: toolGroup(false), active: false });
    expect(view.container.querySelector('.running-label')).toBeNull();
  });

  it.each(scopes)('keeps completed tool groups collapsed in $kind scope', async (scope) => {
    const view = render(ToolCallGroupBlock, {
      row: toolGroup(false),
      scope,
    });
    const button = expansionButton(view.container, 'tool_group');

    await waitFor(() => expect(button).toHaveAttribute('aria-expanded', 'false'));
    await fireEvent.click(button);
    expect(button).toHaveAttribute('aria-expanded', 'true');
  });

  it.each(scopes)('keeps completed activity segments collapsed in $kind scope', async (scope) => {
    const view = render(ActivitySegmentBlock, {
      row: activitySegment(false),
      scope,
    });
    const button = expansionButton(view.container, 'activity_segment');

    await waitFor(() => expect(button).toHaveAttribute('aria-expanded', 'false'));
    await fireEvent.click(button);
    expect(button).toHaveAttribute('aria-expanded', 'true');
  });

  it.each(scopes)('honors explicit tool-group expansion in $kind scope', async (scope) => {
    const view = render(ToolCallGroupBlock, {
      row: toolGroup(true),
      scope,
    });

    await waitFor(() => expect(
      expansionButton(view.container, 'tool_group'),
    ).toHaveAttribute('aria-expanded', 'true'));
  });

  it.each(scopes)('honors explicit activity-segment expansion in $kind scope', async (scope) => {
    const view = render(ActivitySegmentBlock, {
      row: activitySegment(true),
      scope,
    });

    await waitFor(() => expect(
      expansionButton(view.container, 'activity_segment'),
    ).toHaveAttribute('aria-expanded', 'true'));
  });
});
