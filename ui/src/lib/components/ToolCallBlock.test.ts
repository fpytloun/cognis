import { fireEvent, render, screen } from '@testing-library/svelte';
import { describe, expect, it, vi } from 'vitest';

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

describe('ToolCallBlock compact command rendering', () => {
  it('keeps the full command in a horizontally scrollable summary', () => {
    const item: ToolCallTimelineItem = {
      id: 'tool-call:command',
      kind: 'tool_call',
      callId: 'call_command',
      toolName: 'bash',
      status: 'running',
      timestamp: null,
      arguments: {
        command: 'pytest tests/unit/test_a_very_long_command_name.py --verbose --maxfail=1'
      }
    };

    render(ToolCallBlock, {
      item,
      density: 'compact',
      summaryMode: 'command'
    });

    const summary = screen.getByTestId('tool-command-summary-scroll');
    expect(summary.className).toContain('overflow-x-auto');
    expect(summary.className).toContain('command-scroll');
    expect(summary.className).toContain('whitespace-nowrap');
    expect(summary.className).not.toContain('truncate');
    expect(summary.textContent).toContain('pytest tests/unit/test_a_very_long_command_name.py');
  });

  it('uses description mode with command fallback', async () => {
    const item: ToolCallTimelineItem = {
      id: 'tool-call:description',
      kind: 'tool_call',
      callId: 'call_description',
      toolName: 'bash',
      status: 'completed',
      timestamp: null,
      arguments: { command: 'npm test', description: 'Run focused tests' },
      result: 'passed',
    };
    const view = render(ToolCallBlock, {
      item,
      density: 'compact',
      compactLabelMode: 'description',
    });
    expect(screen.getByTestId('tool-command-summary-scroll')).toHaveTextContent('Run focused tests');
    await view.rerender({
      item: { ...item, arguments: { command: 'npm test' } },
      density: 'compact',
      compactLabelMode: 'description',
    });
    expect(screen.getByTestId('tool-command-summary-scroll')).toHaveTextContent('npm test');
    await fireEvent.click(screen.getByText('npm test'));
    expect(screen.getByTestId('tool-terminal-description-scroll')).toHaveClass('command-scroll');
  });

  it('links expanded Work evidence to its source session when available', async () => {
    const onViewSession = vi.fn();
    const item: ToolCallTimelineItem = {
      id: 'tool-call:linked-command',
      kind: 'tool_call',
      callId: 'call_linked_command',
      toolName: 'bash',
      status: 'completed',
      timestamp: null,
      arguments: { command: 'git status' }
    };

    render(ToolCallBlock, {
      item,
      density: 'compact',
      summaryMode: 'command',
      contextLabel: 'laforge · implementation · completed',
      contextSessionId: 'sess_source',
      onViewSession
    });

    await fireEvent.click(screen.getByText('git status'));
    await fireEvent.click(screen.getByRole('button', { name: 'View session' }));
    expect(onViewSession).toHaveBeenCalledWith('sess_source');
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
    expect(screen.getByText('Raw payload')).toBeTruthy();

    await fireEvent.click(screen.getByText('Raw payload'));
    expect(container.textContent).toContain('[[result:1]]');
    expect(container.textContent).toContain('example charts');
  });

  it('keeps failed web fetch raw input and output collapsed by default', async () => {
    const item: ToolCallTimelineItem = {
      id: 'tool-call:web-fetch-error',
      kind: 'tool_call',
      callId: 'call_web_fetch_error',
      toolName: 'web_fetch',
      status: 'failed',
      timestamp: null,
      arguments: {
        url: 'https://example.com/protected',
        diagnostic_marker: 'RAW_FETCH_INPUT',
      },
      result: 'HTTP 403: Forbidden',
      isError: true,
    };

    const { container } = render(ToolCallBlock, { item });
    await fireEvent.click(screen.getByRole('button', { name: /web_fetch/i }));

    expect(screen.getByText('Raw payload')).toBeTruthy();
    expect(container.textContent).not.toContain('RAW_FETCH_INPUT');

    await fireEvent.click(screen.getByText('Raw payload'));
    expect(container.textContent).toContain('RAW_FETCH_INPUT');
    expect(container.textContent).toContain('HTTP 403: Forbidden');
    expect(screen.getByText('Input')).toBeTruthy();
    expect(screen.getByText('Output')).toBeTruthy();
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
