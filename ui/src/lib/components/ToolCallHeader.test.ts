import { cleanup, render, screen } from '@testing-library/svelte';
import { afterEach, describe, expect, it } from 'vitest';
import type { ToolCallTimelineItem } from '$lib/timeline-render-model';
import { delegationIsQueued, toolHeaderDuration } from '$lib/tool-call-header';
import ToolCallHeader from './ToolCallHeader.svelte';
import ToolCallBlock from './ToolCallBlock.svelte';
import { loadToolCallDetails } from './tool-call-details-loader';

// Detail regressions exercise the ready view, not the separately tested lazy loader.
await loadToolCallDetails();

afterEach(cleanup);

const headerBindings = { controlsId: 'delegation-header-details', onclick: () => {} };

function delegate(overrides: Partial<ToolCallTimelineItem> = {}): ToolCallTimelineItem {
  return {
    kind: 'tool_call', id: 'delegate-header', callId: 'delegate-header',
    toolName: 'delegate', arguments: { task: 'Research dashboard patterns' },
    status: 'running', timestamp: '2026-11-03T21:23:00Z',
    ...overrides,
  };
}

describe('delegation admission presentation', () => {
  it('shows queued without a running timer before a child is created', () => {
    const item = delegate();
    render(ToolCallHeader, {
      item, now: Date.parse('2026-11-03T21:38:00Z'), ...headerBindings,
    });
    expect(screen.getByText('Queued')).toBeInTheDocument();
    expect(screen.queryByText('15m 00s')).not.toBeInTheDocument();
    expect(delegationIsQueued(item)).toBe(true);
    expect(toolHeaderDuration(item, Date.parse('2026-11-03T21:38:00Z'))).toBe('');
  });

  it('switches to the actual child start time when admission completes', async () => {
    const view = render(ToolCallHeader, {
      item: delegate(), now: Date.parse('2026-11-03T21:38:00Z'), ...headerBindings,
    });
    const item = delegate({
      delegation: {
        childSessionId: 'child-dashboard', status: 'running',
        startedAt: '2026-11-03T21:37:00Z',
      },
    });
    await view.rerender({ item, now: Date.parse('2026-11-03T21:38:00Z') });
    expect(screen.queryByText('Queued')).not.toBeInTheDocument();
    expect(screen.getByText('1m 00s')).toBeInTheDocument();
  });

  it('preserves approval and terminal states', () => {
    render(ToolCallHeader, {
      item: delegate({ status: 'waiting' }), ...headerBindings,
    });
    expect(screen.getByText('Waiting for approval')).toBeInTheDocument();
    expect(delegationIsQueued(delegate({ status: 'completed' }))).toBe(false);
    expect(delegationIsQueued(delegate({ status: 'failed' }))).toBe(false);
  });

  it('keeps expanded delegation details queued rather than claiming working progress', async () => {
    render(ToolCallBlock, { item: delegate() });
    expect(await screen.findByText(
      'Not started yet. Waiting for earlier work or an available delegation slot.',
    )).toBeInTheDocument();
    expect(screen.queryByText('Working…')).not.toBeInTheDocument();
    expect(screen.queryByText('Running', { exact: true })).not.toBeInTheDocument();
  });
});
