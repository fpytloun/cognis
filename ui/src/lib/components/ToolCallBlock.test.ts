import { fireEvent, render, screen } from '@testing-library/svelte';
import { describe, expect, it } from 'vitest';

import type { ToolCallTimelineItem } from '$lib/timeline-render-model';
import ToolCallBlock from './ToolCallBlock.svelte';

function writeDeliverableItem(): ToolCallTimelineItem {
  return {
    id: 'tool-call:write-deliverable',
    kind: 'tool_call',
    callId: 'call_write_deliverable',
    toolName: 'write_deliverable',
    status: 'completed',
    timestamp: null,
    arguments: {
      title: 'Draft implementation report',
      format: 'rich',
      content: 'SECRET_DRAFT_DELIVERABLE_BODY',
      rich: {
        blocks: [
          { type: 'markdown', content: 'SECRET_RAW_RICH_PAYLOAD' }
        ]
      },
      outputs: {
        changed_files: ['ui/src/lib/components/ToolCallBlock.svelte']
      }
    },
    result: JSON.stringify({
      status: 'buffered',
      deliverable_id: 'dlv_secret',
      version: 4,
      length: 29,
      rich: {
        blocks: [
          { type: 'markdown', content: 'SECRET_NORMALIZED_RICH_PAYLOAD' }
        ]
      }
    })
  };
}

function followUpSubsessionItem(): ToolCallTimelineItem {
  return {
    id: 'tool-call:follow-up-subsession',
    kind: 'tool_call',
    callId: 'call_follow_up',
    toolName: 'follow_up_subsession',
    status: 'running',
    timestamp: null,
    arguments: {
      session_id: 'sess_original',
      instruction: 'Re-review the implementation after the fix.'
    },
    delegation: {
      childSessionId: 'sess_follow_up',
      status: 'running',
      startedAt: '2026-01-01T00:00:00Z'
    }
  };
}

function completedFollowUpSubsessionItem(): ToolCallTimelineItem {
  return {
    ...followUpSubsessionItem(),
    id: 'tool-call:completed-follow-up-subsession',
    status: 'completed',
    delegation: {
      childSessionId: 'sess_follow_up',
      status: 'completed',
      startedAt: '2026-01-01T00:00:00Z'
    }
  };
}

describe('ToolCallBlock write_deliverable rendering', () => {
  it('keeps intermediate deliverable tool cards compact and exposes explicit raw and preview controls', async () => {
    const { container } = render(ToolCallBlock, { item: writeDeliverableItem() });

    expect(screen.getByText('Deliverable captured')).toBeTruthy();
    expect(screen.getAllByText('Draft implementation report').length).toBeGreaterThan(0);
    expect(screen.getByText('Final deliverable renders at turn end.')).toBeTruthy();
    expect(screen.getByText('dlv_secret')).toBeTruthy();
    expect(screen.getByText('rich')).toBeTruthy();
    expect(screen.getByText('buffered')).toBeTruthy();

    expect(screen.getByText('Raw payload')).toBeTruthy();
    expect(container.textContent).not.toContain('SECRET_DRAFT_DELIVERABLE_BODY');
    expect(container.textContent).not.toContain('SECRET_RAW_RICH_PAYLOAD');
    expect(container.textContent).not.toContain('SECRET_NORMALIZED_RICH_PAYLOAD');

    await fireEvent.click(screen.getByText('Raw payload'));
    expect(container.textContent).toContain('SECRET_DRAFT_DELIVERABLE_BODY');
    expect(container.textContent).toContain('SECRET_RAW_RICH_PAYLOAD');
    expect(container.textContent).toContain('SECRET_NORMALIZED_RICH_PAYLOAD');

    await fireEvent.click(screen.getByRole('button', { name: 'View deliverable' }));
    const preview = screen.getByTitle('Deliverable preview');
    expect(preview.getAttribute('src')).toBe('/api/v1/deliverables/dlv_secret/view');
  });
});

describe('ToolCallBlock web rendering', () => {
  it('renders structured web search results and lazy image references', async () => {
    const item: ToolCallTimelineItem = {
      id: 'tool-call:web-search',
      kind: 'tool_call',
      callId: 'call_web_search',
      toolName: 'web_search',
      status: 'completed',
      timestamp: null,
      arguments: { query: 'example charts', include_images: true },
      result: [
        '[[result:1]]',
        '[1] Example chart',
        '    URL: https://example.com/chart',
        '    Snippet: Quarterly results.',
        '',
        '[[media:1]]',
        'URL: https://cdn.example.com/chart.png',
        'Lazy artifact: tool_artifact:call_web_search:media:1',
      ].join('\n'),
    };

    const { container } = render(ToolCallBlock, { item });
    await fireEvent.click(screen.getByRole('button', { name: /web_search/i }));

    expect(screen.getByText('Web search')).toBeTruthy();
    expect(screen.getByText('Example chart')).toBeTruthy();
    expect(screen.getByText('Image references')).toBeTruthy();
    expect(screen.getByText('lazy artifact available')).toBeTruthy();
    expect(container.textContent).not.toContain('[[result:1]]');
  });
});

describe('ToolCallBlock delegation lineage rendering', () => {
  it('renders a follow-up subsession with the delegated sub-session card', () => {
    render(ToolCallBlock, { item: followUpSubsessionItem() });

    expect(screen.getByText('Delegated sub-session')).toBeTruthy();
    expect(screen.getAllByText('Re-review the implementation after the fix.')).toHaveLength(2);
    expect(screen.getByText('sess_follow_up')).toBeTruthy();
  });

  it('uses the instruction in a terminal follow-up subsession header', () => {
    render(ToolCallBlock, { item: completedFollowUpSubsessionItem() });

    expect(screen.getByText('Re-review the implementation after the fix.')).toBeTruthy();
    expect(screen.queryByText('Delegated sub-session')).toBeNull();
  });
});
