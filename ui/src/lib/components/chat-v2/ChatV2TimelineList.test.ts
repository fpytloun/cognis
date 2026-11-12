import { cleanup, fireEvent, render, waitFor } from '@testing-library/svelte';
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import ChatV2TimelineList from './ChatV2TimelineList.svelte';
import { conversationTimelineScope, type ToolCallTimelineItem, type MessageTimelineItem } from '$lib/chat-v2/types';
import { DEFAULT_USER_PREFERENCES } from '$lib/user-preferences';
import { loginFixtureAccount } from '../../../test/verified-account';
import { auth } from '$lib/stores/auth';

const ungrouped = {
  ...DEFAULT_USER_PREFERENCES,
  chat: { ...DEFAULT_USER_PREFERENCES.chat, group_tool_calls: false }
};

const tool = (index: number): ToolCallTimelineItem => ({
  id: `tool:${index}`, kind: 'tool_call', sort_key: String(index).padStart(8, '0'),
  source_refs: [], stable: true, status: 'complete', call_id: `call-${index}`,
  tool_name: 'read', turn_id: 'turn-1', turn_cycle_index: 0, assistant_phase_index: 0,
  arguments: null, is_error: false, attachments: [], file_diffs: [], truncated: false,
  has_full_output: false,
});

beforeAll(async () => { await import('$lib/components/ToolCallDetails.svelte'); });
afterEach(async () => { cleanup(); await auth.clear(); });

describe('windowed timeline disclosure continuity', () => {
  it('shows successful comment replies in the assistant response without expanding tool activity', async () => {
    await loginFixtureAccount();
    const changed = vi.fn();
    window.addEventListener('feedback-changed', changed);
    const reply: ToolCallTimelineItem = {
      ...tool(1), source_refs: [{ store: 'intaris', session_id: 'session-feedback', seq: 1, event_type: 'tool_call' }], tool_name: 'comment_reply',
      arguments: { comment_id: 'fbt_reviewed', reply: 'Kept the implementation **simple**.', resolve: true },
      result_preview: '{"replied":true,"state":"resolved"}',
    };
    const answer = {
      id: 'message-answer', kind: 'message', role: 'assistant', content: 'Answered the comments.',
      sort_key: '00000002', source_refs: [{ store: 'intaris', session_id: 'session-feedback', seq: 2, event_type: 'message' }], stable: true, status: 'complete',
      turn_id: 'turn-1',
    } as MessageTimelineItem;
    try {
      const view = render(ChatV2TimelineList, { items: [reply, answer] });
      const replies = await view.findByRole('region', { name: 'Comment replies' });
      expect(replies).toHaveTextContent('Kept the implementation simple.');
      expect(replies.querySelector('strong')).toHaveTextContent('simple');
      expect(replies).toHaveTextContent('Resolved');
      expect(changed).toHaveBeenCalledOnce();
      await view.rerender({ items: [reply, { ...answer, content: 'All comments answered.' }] });
      expect(changed).toHaveBeenCalledOnce();
    } finally { window.removeEventListener('feedback-changed', changed); }
  });
  it('renders a python execution with inspectable source, output, and linked tool activity', async () => {
    const parent: ToolCallTimelineItem = {
      ...tool(10),
      tool_name: 'python_execute',
      status: 'complete',
      arguments: {
        description: 'Read the report and count rows.',
        code: 'rows = await cognis.tools.read(file_path="report.csv")\nprint(len(rows))'
      },
      result_preview: '{"row_count": 12}',
      program_id: 'program-10',
      kernel_generation: 2
    };
    const child: ToolCallTimelineItem = {
      ...tool(11),
      tool_name: 'read',
      status: 'complete',
      arguments: { file_path: 'report.csv' },
      result_preview: 'id,name\\n1,Example',
      parent_call_id: parent.call_id,
      program_id: 'program-10',
      kernel_generation: 2
    };
    const view = render(ChatV2TimelineList, { items: [parent, child], preferences: ungrouped });
    let activity!: HTMLElement;
    await waitFor(() => {
      activity = view.container.querySelector<HTMLElement>('[data-kind="python_execution"]')!;
      expect(activity).toBeTruthy();
    });

    expect(activity).toHaveAttribute('data-python-execution-status', 'completed');
    expect(activity).toHaveTextContent('Python execution');
    expect(activity).toHaveTextContent('Read the report and count rows.');
    expect(activity.querySelector('button[aria-expanded]')).toHaveAttribute('aria-expanded', 'false');
    expect(activity).not.toHaveTextContent('id,name');

    await fireEvent.click(activity.querySelector('button[aria-expanded]')!);

    await waitFor(() => expect(activity.querySelector('[data-testid="python-execution-code"]')).toBeTruthy());
    expect(activity.querySelector('[data-testid="python-execution-code"]')).toHaveTextContent('await cognis.tools.read');
    expect(activity.querySelector('[data-testid="python-execution-result"]')).toHaveTextContent('row_count');
    expect(activity).toHaveTextContent('Nested tool activity');
    expect(activity).toHaveTextContent('report.csv');
    expect(activity.querySelectorAll('article.tool-frame')).toHaveLength(2);
  });

  it('surfaces linked approval while collapsed and keeps the child card actionable when opened', async () => {
    const parent: ToolCallTimelineItem = {
      ...tool(20),
      tool_name: 'python_execute',
      status: 'running',
      arguments: { description: 'Look up one approved record.', code: 'await cognis.tools.read(...)' },
      program_id: 'program-20',
      kernel_generation: 3
    };
    const child: ToolCallTimelineItem = {
      ...tool(21),
      tool_name: 'read',
      status: 'waiting',
      parent_call_id: parent.call_id,
      program_id: 'program-20',
      kernel_generation: 3
    };
    const view = render(ChatV2TimelineList, { items: [parent, child], preferences: ungrouped });
    let activity!: HTMLElement;
    await waitFor(() => {
      activity = view.container.querySelector<HTMLElement>('[data-kind="python_execution"]')!;
      expect(activity).toBeTruthy();
    });

    expect(activity).toHaveAttribute('data-python-execution-status', 'waiting');
    expect(activity).toHaveTextContent('Waiting for approval');
    await waitFor(() => expect(activity.querySelector('[data-testid="python-execution-code"]')).toBeTruthy());
    expect(activity.querySelectorAll('article.tool-frame')).toHaveLength(2);

    const inspector = activity.querySelector<HTMLButtonElement>('button[aria-expanded]')!;
    expect(inspector).toHaveAttribute('aria-expanded', 'true');
    await fireEvent.click(inspector);
    expect(inspector).toHaveAttribute('aria-expanded', 'false');
    expect(activity).toHaveTextContent('Waiting for approval');
    await fireEvent.click(inspector);
    expect(inspector).toHaveAttribute('aria-expanded', 'true');
    await waitFor(() => expect(activity.querySelectorAll('article.tool-frame')).toHaveLength(2));
    const nestedCard = activity.querySelectorAll<HTMLElement>('article.tool-frame')[1];
    expect(nestedCard.querySelector('button')).toBeTruthy();
  });

  it('renders Python execution failures with an error result state', async () => {
    const item: ToolCallTimelineItem = {
      ...tool(30),
      tool_name: 'python_execute',
      status: 'failed',
      arguments: { description: 'Summarize the report.', code: 'raise ValueError("bad report")' },
      result_preview: 'ValueError: bad report',
      is_error: true
    };
    const view = render(ChatV2TimelineList, { items: [item], preferences: ungrouped });
    let activity!: HTMLElement;
    await waitFor(() => {
      activity = view.container.querySelector<HTMLElement>('[data-kind="python_execution"]')!;
      expect(activity).toBeTruthy();
    });

    expect(activity).toHaveAttribute('data-python-execution-status', 'failed');
    await waitFor(() => expect(activity.querySelector('[data-testid="python-execution-result"]')).toBeTruthy());
    expect(activity.querySelector('[data-testid="python-execution-result"]')).toHaveTextContent('ValueError: bad report');
    expect(activity).toHaveTextContent('Execution error');
  });

  it('shows incremental output while running and retains stdout, result and nested cards after completion', async () => {
    const parent: ToolCallTimelineItem = {
      ...tool(40), tool_name: 'python_execute', status: 'running',
      arguments: { description: 'Print progress and count rows.', code: 'print("first", flush=True)' },
      streamed_output: 'first\n',
    };
    const view = render(ChatV2TimelineList, { items: [parent] });
    const activity = () => view.container.querySelector<HTMLElement>('[data-kind="python_execution"]')!;
    await waitFor(() => expect(activity()?.querySelector('[data-testid="python-execution-output"]')).toHaveTextContent('first'));
    expect(view.container.querySelector('[data-tool-group-kind="code"]')).toHaveTextContent('Running code');
    expect(activity()).toHaveAttribute('data-python-execution-status', 'running');
    await view.rerender({ items: [{ ...parent, streamed_output: 'first\nsecond\n' }] });
    await waitFor(() => expect(activity().querySelector('[data-testid="python-execution-output"]')).toHaveTextContent('first second'));
    const envelope = JSON.stringify({
      protocol_version: 'cognis.program.v1', status: 'completed',
      stdout_preview: 'first\nsecond\nlast\n', result: { count: 3 }, kernel_generation: 4
    });
    await view.rerender({ items: [{ ...parent, status: 'complete', result_preview: envelope, streamed_output: null }] });
    await waitFor(() => expect(activity().querySelector('[data-testid="python-execution-output"]')).toHaveTextContent('first second last'));
    expect(activity().querySelector('[data-testid="python-execution-result"]')).toHaveTextContent('"count": 3');
    expect(activity().querySelectorAll('article.tool-frame')).toHaveLength(1);
    expect(activity().querySelector('[data-testid="python-execution-code"]')).toHaveTextContent('print("first", flush=True)');
    expect(activity().querySelector('details')?.open).toBe(false);
    expect(activity()).not.toHaveTextContent('Python source and activity');
    expect(activity()).not.toHaveTextContent('Input');
    // A canonical snapshot with no live overlay still retains terminal output.
    const reloaded = render(ChatV2TimelineList, {
      items: [{ ...parent, status: 'complete', result_preview: envelope, streamed_output: null }],
      preferences: ungrouped,
    });
    await waitFor(() => expect(reloaded.container.querySelector('button[aria-expanded]')).toBeTruthy());
    await fireEvent.click(reloaded.container.querySelector<HTMLButtonElement>('button[aria-expanded]')!);
    await waitFor(() => expect(reloaded.container.querySelector('[data-testid="python-execution-output"]')).toHaveTextContent('first second last'));
  });

  it('supports callers without an optional conversation scope', () => {
    const view = render(ChatV2TimelineList, { items: [tool(1), tool(2)] });
    expect(view.container.querySelector('[data-timeline-row-key]')).toBeTruthy();
  });

  it('keeps expanded tools accessible when backfill adds a containing activity segment', async () => {
    const scope = conversationTimelineScope('conversation-1');
    const view = render(ChatV2TimelineList, { items: [tool(2), tool(3)], scope });
    await fireEvent.click(view.container.querySelector<HTMLButtonElement>('button[aria-expanded]')!);
    const preface: MessageTimelineItem = {
      id: 'preface:1', kind: 'message', message_id: 'preface:1', role: 'assistant',
      sort_key: '00000001', source_refs: [], stable: true, partial: false,
      content: 'I will inspect this.', turn_id: 'turn-1', turn_cycle_index: 0, attachments: [],
    };
    await view.rerender({ items: [preface, tool(2), tool(3)], scope });
    expect(view.container.querySelector('[data-timeline-row-key]')?.getAttribute('data-timeline-row-key'))
      .toContain('activity-segment');
    expect(view.container.querySelector('[data-timeline-item-id="tool:2"]')).toBeTruthy();
  });

  it('preserves explicit expansion when prepending changes the group key', async () => {
    const scope = conversationTimelineScope('conversation-1');
    const view = render(ChatV2TimelineList, { items: [tool(2), tool(3)], scope });
    const header = () => view.container.querySelector<HTMLButtonElement>('[data-timeline-row-key] button[aria-expanded]')!;
    await fireEvent.click(header());
    expect(header().getAttribute('aria-expanded')).toBe('true');
    await view.rerender({ items: [tool(1), tool(2), tool(3)], scope });
    expect(header().getAttribute('aria-expanded')).toBe('true');
    expect(view.container.querySelector('[data-timeline-item-id="tool:2"]')).toBeTruthy();
  });

  it('carries the choice to new members before the originally toggled members are evicted', async () => {
    const scope = conversationTimelineScope('conversation-1');
    const view = render(ChatV2TimelineList, { items: [tool(1), tool(2)], scope });
    const header = () => view.container.querySelector<HTMLButtonElement>('[data-timeline-row-key] button[aria-expanded]')!;
    await fireEvent.click(header());
    await view.rerender({ items: [tool(1), tool(2), tool(3), tool(4)], scope });
    await view.rerender({ items: [tool(3), tool(4)], scope });
    expect(header().getAttribute('aria-expanded')).toBe('true');
    expect(view.container.querySelector('[data-timeline-item-id="tool:3"]')).toBeTruthy();
  });

  it('does not reuse disclosure choices in another conversation', async () => {
    const view = render(ChatV2TimelineList, {
      items: [tool(1), tool(2)], scope: conversationTimelineScope('conversation-1'),
    });
    const header = () => view.container.querySelector<HTMLButtonElement>('[data-timeline-row-key] button[aria-expanded]')!;
    await fireEvent.click(header());
    await view.rerender({ items: [tool(1), tool(2)], scope: conversationTimelineScope('conversation-2') });
    expect(header().getAttribute('aria-expanded')).toBe('false');
  });
});
