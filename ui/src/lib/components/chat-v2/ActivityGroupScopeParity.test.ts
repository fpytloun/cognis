import { cleanup, fireEvent, render, waitFor } from '@testing-library/svelte';
import { afterEach, describe, expect, it } from 'vitest';

import type { ActivitySegmentRow, ToolGroupRow } from '$lib/chat-v2/tool-groups';
import type { TimelineScope, ToolCallTimelineItem } from '$lib/chat-v2/types';
import ActivitySegmentBlock from './ActivitySegmentBlock.svelte';
import ToolCallGroupBlock from './ToolCallGroupBlock.svelte';

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
