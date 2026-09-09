import { fireEvent, render, screen } from '@testing-library/svelte';
import { describe, expect, it, vi } from 'vitest';
import InspectorOverview from './InspectorOverview.svelte';
import type { ActivityOverviewResponse } from '$lib/chat-v2/types';
import { canonicalWorkstreamSessionId, workstreamForSession } from '$lib/inspectorTreeNavigation';

const overview: ActivityOverviewResponse = {
  schema_version: 2, projection_version: 'v1',
  scope: { key: 'conversation:c1', kind: 'conversation', conversation_id: 'c1' },
  summary: { changed_files: 4, commands: 3, mutations: 2, artifacts: 1 },
  materialization: { state: 'live' },
  workstreams: [], graph_fingerprint: 'graph', graph_truncated: false,
  recent: { commands: Array.from({ length: 10 }, (_, index) => ({ id: `c${index}`, category: 'commands', session_id: 's1', occurred_at: `2026-01-01T00:00:${String(index).padStart(2, '0')}Z`, title: `Command ${index}` })) },
};

describe('InspectorOverview live lifecycle', () => {
  it('shows No activity yet for an empty live response, not zero metric cards', () => {
    const empty = {
      ...overview,
      summary: { changed_files: 0, commands: 0, mutations: 0, artifacts: 0 },
      workstreams: [],
      recent: {},
      materialization: { state: 'live' as const },
    };
    render(InspectorOverview, { overview: empty });
    expect(screen.getByTestId('activity-overview-empty')).toHaveTextContent('No activity yet.');
    expect(screen.queryByTestId('activity-summary-strip')).toBeNull();
    expect(screen.queryByText(/^0 files$/)).toBeNull();
  });

  it('keeps retained content visible with the shared catch-up status', () => {
    const catchingUp = {
      ...overview,
      materialization: { state: 'catching_up' as const },
    };
    render(InspectorOverview, { overview: catchingUp });
    expect(screen.getByTestId('activity-summary-strip')).toBeTruthy();
    const banner = screen.getByTestId('activity-lifecycle-catching_up');
    expect(banner).toHaveTextContent('Catching up activity…');
    expect(banner).toHaveAttribute('role', 'status');
    expect(banner).toHaveAttribute('aria-busy', 'true');
  });

  it('shows partial and failed states while retaining content', async () => {
    const { rerender } = render(InspectorOverview, {
      overview: { ...overview, materialization: { state: 'partial' } },
    });
    expect(screen.getByTestId('activity-lifecycle-partial')).toHaveTextContent('Activity is partially available.');
    expect(screen.getByTestId('activity-summary-strip')).toBeTruthy();
    const onRefresh = vi.fn();
    await rerender({
      overview: { ...overview, materialization: { state: 'failed' } },
      onRefresh,
    });
    expect(screen.getByTestId('activity-lifecycle-failed')).toHaveTextContent('Activity refresh failed.');
    await fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(onRefresh).toHaveBeenCalled();
  });

  it('shows only the failed lifecycle when an empty projection fails', () => {
    const failed = {
      ...overview,
      summary: { changed_files: 0, commands: 0, mutations: 0, artifacts: 0 },
      workstreams: [],
      recent: {},
      materialization: { state: 'failed' as const },
    };
    render(InspectorOverview, { overview: failed });
    expect(screen.getByTestId('activity-lifecycle-failed')).toBeTruthy();
    expect(screen.queryByTestId('activity-overview-empty')).toBeNull();
    expect(screen.queryByTestId('activity-summary-strip')).toBeNull();
  });

  it('renders normally with no banner when live', () => {
    render(InspectorOverview, { overview });
    expect(screen.queryByTestId(/^activity-lifecycle-/)).toBeNull();
    expect(screen.getByTestId('activity-summary-strip')).toBeTruthy();
  });
});

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
      diagnosticsFreshness: 'current',
    });
    expect(screen.getByTestId('overview-context-window')).toHaveTextContent('62,646 / 500,000 (13%)');
    expect(screen.getAllByText('Running').length).toBeGreaterThan(0);
    expect(screen.getAllByText('Lumi Agent').length).toBeGreaterThan(0);
    expect(screen.getAllByTestId('activity-avatar').length).toBeGreaterThan(0);
    expect(screen.getByText('developer-senior')).toBeTruthy();
    expect(screen.getByText('profile-model')).toBeTruthy();
    expect(screen.getByText('Thinking high')).toBeTruthy();
    expect(screen.getByTestId('overview-context-freshness')).toHaveTextContent('Live projected estimate');
    expect(screen.getByTestId('overview-stats-scope')).toHaveTextContent('Aggregate for this activity graph');
    expect(screen.getByText('Execution sessions')).toBeTruthy();
    await fireEvent.click(screen.getByRole('button', { name: 'Copy session ID' }));
    expect(writeText).toHaveBeenCalledWith('s1');
  });

  it('does not label the prior turn runtime as current before the new turn assembles', () => {
    const pendingOverview = {
      ...overview,
      workstreams: [{
        key: 'root', root_key: 'root', parent_key: null, kind: 'conversation', edge_kind: 'contains', ordinal: 0,
        session_id: 's1', event_store_session_id: 's1', title: 'Focused work', agent_id: 'lumi',
        agent_profile_id: 'smart', model: 'gpt-5.6-sol', reasoning_effort: 'high', status: 'active', current: true,
        superseded: false, activity_state: 'ongoing', execution_state: 'running',
        active_turn_id: 'turn-new', execution_turn_id: 'turn-old',
      }],
    } as ActivityOverviewResponse;
    render(InspectorOverview, {
      overview: pendingOverview,
      focusedSession: pendingOverview.workstreams[0],
      diagnosticsFreshness: 'updating',
      contextUsage: {
        model: 'gpt-5.6-sol',
        reasoning_effort: 'high',
        prompt_tokens: 309_750,
        max_context_tokens: 500_000,
        percentage: 62,
        turn_id: 'turn-old',
      },
    });
    expect(screen.getByText('Model resolving…')).toBeTruthy();
    expect(screen.queryByText('gpt-5.6-sol')).toBeNull();
    expect(screen.getByTestId('overview-context-freshness')).toHaveTextContent(
      'Updating after prompt assembly',
    );
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

  it('keeps terminated focused sessions terminal over runtime activity', () => {
    const focused = {
      ...overview.workstreams[0],
      status: 'terminated',
      activity_state: 'ongoing' as const,
    };
    render(InspectorOverview, {
      overview,
      focusedSession: focused,
      focusedSessionId: focused.session_id,
      focusedSessionRuntimeActive: true,
      runtimeActiveSessionIds: [focused.session_id],
    });
    expect(screen.getAllByText('Closed').length).toBeGreaterThan(0);
    expect(screen.queryByText('Running')).toBeNull();
    expect(screen.queryByTestId('activity-avatar-orbit')).toBeNull();
  });

  it('keeps failed terminal status over aggregate ongoing and exposes narrow container structure', () => {
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
    expect(screen.getAllByText('Failed').length).toBeGreaterThan(0);
    expect(screen.queryByText('Running')).toBeNull();
    expect(screen.getByTestId('focused-session-card')).toHaveClass('focused-session-card');
    expect(screen.getByTestId('focused-session-card').querySelector('.focused-session-metadata')).toBeTruthy();
  });

  it.each([
    ['running', 'Running'],
    ['queued', 'Queued'],
    ['waiting', 'Waiting'],
    ['recovering', 'Recovering'],
    ['idle', 'Idle'],
    ['completed', 'Completed'],
  ] as const)('uses canonical %s state in both focused header and tree', (executionState, label) => {
    const focused = {
      key: 'root', root_key: 'root', parent_key: null, kind: 'conversation' as const,
      edge_kind: 'contains' as const, ordinal: 0, session_id: 's1',
      event_store_session_id: 's1', title: 'Current work', agent_id: 'lumi',
      current: true, superseded: false,
      status: 'completed',
      activity_state: 'closed' as const,
      execution_state: executionState,
    };
    render(InspectorOverview, {
      overview: { ...overview, workstreams: [focused] },
      focusedSession: focused,
      focusedSessionId: focused.session_id,
      focusedSessionRuntimeActive: true,
      runtimeActiveSessionIds: [focused.session_id],
    });
    expect(screen.getByTestId('focused-session-card')).toHaveTextContent(label);
    expect(screen.getByTestId('workstream-execution-status')).toHaveTextContent(label);
    expect(screen.queryByText('Closed')).toBeNull();
  });

  it('uses live runtime activity for a nonterminal focused session', () => {
    const focused = { ...overview.workstreams[0], status: 'active', activity_state: 'active' as const };
    render(InspectorOverview, {
      overview,
      focusedSession: focused,
      focusedSessionId: focused.session_id,
      focusedSessionRuntimeActive: true,
      runtimeActiveSessionIds: [focused.session_id],
    });
    expect(screen.getAllByText('Running').length).toBeGreaterThan(0);
    expect(screen.getAllByTestId('activity-avatar-orbit').length).toBeGreaterThan(0);
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
    // Root auto-expands because the focused session is its descendant.
    expect(
      screen.getByTestId('activity-node-logical-child').querySelector('.ring-1'),
    ).toBeNull();
    expect(screen.getByTestId('activity-node-logical-child').firstElementChild).toHaveClass('border-sky-400/60');
  });
});
