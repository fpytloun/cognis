import { fireEvent, render, screen } from '@testing-library/svelte';
import { describe, expect, it, vi } from 'vitest';
import InspectorOverview from './InspectorOverview.svelte';
import type { ActivityOverviewResponse } from '$lib/chat-v2/types';
import { canonicalWorkstreamSessionId, workstreamForSession } from '$lib/inspectorTreeNavigation';

const overview: ActivityOverviewResponse = {
  schema_version: 2, projection_version: 'v1',
  scope: { key: 'conversation:c1', kind: 'conversation', conversation_id: 'c1' },
  summary: { changed_files: 4, commands: 3, mutations: 2, artifacts: 1 },
  materialization: { state: 'caught_up', completed_streams: 1, total_streams: 1, covered_events: 1, target_events: 1, failed_streams: 0 },
  workstreams: [], graph_fingerprint: 'graph', graph_truncated: false,
  recent: { commands: Array.from({ length: 10 }, (_, index) => ({ id: `c${index}`, category: 'commands', session_id: 's1', occurred_at: `2026-01-01T00:00:${String(index).padStart(2, '0')}Z`, title: `Command ${index}` })) },
};

describe('InspectorOverview', () => {
  it('uses five recent rows when narrow and links categories to Work', async () => {
    const onOpenWork = vi.fn();
    render(InspectorOverview, { overview, narrow: true, onOpenWork });
    expect(screen.getByTestId('recent-activity-list').querySelectorAll('li')).toHaveLength(5);
    await fireEvent.click(screen.getByText('Open Work'));
    expect(onOpenWork).toHaveBeenCalledWith('files', undefined);
    await fireEvent.click(screen.getByRole('button', { name: /Commands/ }));
    expect(onOpenWork).toHaveBeenCalledWith('commands', undefined);
    expect(screen.getByTestId('overview-context-window')).toHaveTextContent('Unavailable');
  });

  it('renders agent, profile, model, thinking, live status, and copyable session ID', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText },
    });
    const withSession = {
      ...overview,
      workstreams: [{
        key: 'root', root_key: 'root', parent_key: null, kind: 'conversation', edge_kind: 'contains', ordinal: 0,
        session_id: 's1', event_store_session_id: 's1', title: 'Focused work', agent_id: 'lumi',
        agent_profile_id: 'developer-senior', model: 'profile-model', reasoning_effort: 'high', status: 'idle', current: true,
        superseded: false, activity_state: 'ongoing',
      }],
    } as ActivityOverviewResponse;
    render(InspectorOverview, {
      overview: withSession,
      focusedSession: withSession.workstreams[0],
      agents: [{ agent_id: 'lumi', display_name: 'Lumi Agent', avatar_url: '/avatar.png' }],
      contextUsage: { model: 'test', agent_profile_id: 'live-profile', reasoning_effort: 'low', prompt_tokens: 62_646, max_input_tokens: 10, max_context_tokens: 500_000, percentage: 99 },
      contextSessionId: 's1',
    });
    expect(screen.getByTestId('overview-context-window')).toHaveTextContent('62,646 / 500,000 (13%)');
    expect(screen.getAllByText('Running').length).toBeGreaterThan(0);
    expect(screen.getAllByText('Lumi Agent').length).toBeGreaterThan(0);
    expect(screen.getAllByTestId('activity-avatar').length).toBeGreaterThan(0);
    expect(screen.getByText('live-profile')).toBeTruthy();
    expect(screen.getByText('test')).toBeTruthy();
    expect(screen.getByText('Thinking low')).toBeTruthy();
    expect(screen.getByText('Execution sessions')).toBeTruthy();
    await fireEvent.click(screen.getByRole('button', { name: 'Copy session ID' }));
    expect(writeText).toHaveBeenCalledWith('s1');
  });

  it('keeps terminal status authoritative over stale runtime state', () => {
    const terminalOverview = {
      ...overview,
      workstreams: [{
        key: 'root', root_key: 'root', parent_key: null, kind: 'conversation', edge_kind: 'contains', ordinal: 0,
        session_id: 's1', event_store_session_id: 's1', title: 'Done', agent_id: 'lumi',
        agent_profile_id: 'developer', model: 'test-model', reasoning_effort: 'low',
        status: 'completed', current: true, superseded: false, activity_state: 'closed',
      }],
    } as ActivityOverviewResponse;
    render(InspectorOverview, {
      overview: terminalOverview,
      focusedSession: terminalOverview.workstreams[0],
      focusedSessionRuntimeActive: true,
    });
    expect(screen.getAllByText('Closed').length).toBeGreaterThan(0);
    expect(screen.queryByText('Running')).toBeNull();
  });

  it('uses aggregate ongoing over failed canonical status and exposes narrow container structure', () => {
    const focused = {
      ...overview.workstreams[0],
      status: 'failed',
      activity_state: 'ongoing' as const,
    };
    render(InspectorOverview, {
      overview,
      focusedSession: focused,
      focusedSessionId: focused.session_id,
    });
    expect(screen.getAllByText('Running').length).toBeGreaterThan(0);
    expect(screen.queryByText('Failed')).toBeNull();
    expect(screen.getByTestId('focused-session-card')).toHaveClass('focused-session-card');
    expect(screen.getByTestId('focused-session-card').querySelector('.focused-session-metadata')).toBeTruthy();
  });

  it('renders logical session details for an old backing session without a loading placeholder', async () => {
    const logicalOverview = {
      ...overview,
      workstreams: [
        {
          key: 'root', root_key: 'root', parent_key: null, kind: 'conversation',
          edge_kind: 'contains', ordinal: 0, session_id: 'root-session',
          event_store_session_id: 'root-session', title: 'Root', agent_id: 'lumi',
          status: 'completed', current: false, superseded: false, activity_state: 'closed',
        },
        {
          key: 'logical-child', root_key: 'root', parent_key: 'root', kind: 'managed_agent',
          edge_kind: 'contains', ordinal: 1, session_id: 'canonical-child',
          backing_session_ids: ['old-child'], event_store_session_id: 'canonical-child',
          title: 'Logical child', agent_id: 'lumi', status: 'completed', current: false,
          superseded: false, activity_state: 'closed',
        },
      ],
    } as ActivityOverviewResponse;
    const requestedSessionId = 'old-child';
    const focusedSession = workstreamForSession(logicalOverview.workstreams, requestedSessionId);
    const focusedSessionId = canonicalWorkstreamSessionId(
      logicalOverview.workstreams,
      requestedSessionId,
    );

    render(InspectorOverview, {
      overview: logicalOverview,
      focusedSession,
      focusedSessionId,
    });

    expect(screen.getByTestId('focused-session-card')).toHaveTextContent('Logical child');
    expect(screen.queryByText('Loading session details…')).toBeNull();
    await fireEvent.click(screen.getByRole('button', { name: 'Expand Root' }));
    expect(
      screen.getByTestId('activity-node-logical-child').querySelector('.ring-1'),
    ).toBeNull();
    expect(screen.getByTestId('activity-node-logical-child').firstElementChild).toHaveClass('border-sky-400/60');
  });
});
