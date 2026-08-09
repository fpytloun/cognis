import { fireEvent, render, screen } from '@testing-library/svelte';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import ActivityTree from './ActivityTree.svelte';
import type { WorkstreamRef } from '$lib/chat-v2/types';
import { traverseInspectorSession, type InspectorTraversalState } from '$lib/inspectorTreeNavigation';

function node(key: string, parent_key: string | null, state: 'ongoing' | 'active' | 'closed', files = 2): WorkstreamRef {
  return {
    key, parent_key, root_key: 'root', kind: key === 'child' ? 'managed_agent' : 'conversation',
    edge_kind: 'contains', ordinal: key === 'root' ? 0 : 1, session_id: `session-${key}`,
    event_store_session_id: `session-${key}`, title: key, agent_id: 'agent', status: state,
    current: state === 'active', superseded: false, activity_state: state,
    summary: { changed_files: files, commands: 3, mutations: 0, artifacts: 0, additions: files ? 5 : 0, deletions: files ? 2 : 0 },
  };
}

describe('ActivityTree', () => {
  beforeEach(() => localStorage.clear());
  it('renders topology, expands ongoing paths, and links exact session Work', async () => {
    const onViewWork = vi.fn();
    const onViewSession = vi.fn();
    render(ActivityTree, {
      nodes: [node('root', null, 'active'), { ...node('child', 'root', 'ongoing'), agent_profile_id: 'developer-senior' }],
      agents: [{ agent_id: 'agent', display_name: 'Agent Display', avatar_url: '/agent.png' }],
      focusedSessionId: 'session-child', onViewWork, onViewSession,
    });
    expect(screen.getByTestId('activity-node-child')).toBeTruthy();
    await fireEvent.click(screen.getByRole('button', { name: 'View Work for child' }));
    expect(onViewWork).toHaveBeenCalledWith('session-child', 'files');
    await fireEvent.click(screen.getByRole('button', { name: 'View session child' }));
    expect(onViewSession).toHaveBeenCalledWith('session-child', expect.objectContaining({ key: 'child' }));
    expect(screen.queryByText(/2F|3C/)).toBeNull();
    expect(screen.getAllByText('2 files')).toHaveLength(2);
    expect(screen.getAllByText('Agent Display').length).toBeGreaterThan(0);
    expect(screen.getByText(/developer-senior/)).toBeTruthy();
    expect(screen.getByText('Running')).toBeTruthy();
    expect(screen.getAllByTestId('activity-avatar-orbit')).toHaveLength(1);
    expect(screen.getByRole('button', { name: 'View session child' })).toHaveClass('scrollbar-hidden-x');
    const childRow = screen.getByTestId('activity-node-child');
    expect(childRow.querySelector('.activity-tree-title-row [data-testid="activity-avatar"]')).toBeNull();
    expect(childRow.querySelector('.activity-tree-metadata-row [data-testid="activity-avatar"]')).toBeTruthy();
    expect(childRow.querySelector('.activity-tree-branch')).toBeTruthy();
    expect(screen.getByTestId('activity-node-root').firstElementChild?.querySelector(':scope > .activity-tree-guides > .activity-tree-branch')).toBeNull();
    expect(childRow.firstElementChild).toHaveStyle('--tree-depth: 1');
  });

  it('connects a single child to the root with a terminating elbow', () => {
    render(ActivityTree, {
      nodes: [node('root', null, 'closed'), node('only-child', 'root', 'ongoing')],
    });
    const rootGuides = screen.getByTestId('activity-node-root').querySelector('.activity-tree-guides')!;
    const childGuides = screen.getByTestId('activity-node-only-child').querySelector('.activity-tree-guides')!;
    expect(rootGuides.querySelector('[data-guide-role="branch-connector"]')).toBeNull();
    expect(rootGuides.querySelector('[data-guide-role="child-trunk"]')).toBeTruthy();
    expect(childGuides.querySelector('[data-guide-role="parent-trunk-before"]')).toBeTruthy();
    expect(childGuides.querySelector('[data-guide-role="branch-connector"]')).toBeTruthy();
    expect(childGuides.querySelector('[data-guide-role="parent-trunk-after"]')).toBeNull();
  });

  it('continues the parent trunk through middle children and terminates at the last connector', () => {
    render(ActivityTree, {
      nodes: [
        node('root', null, 'closed'),
        node('first', 'root', 'closed'),
        node('middle', 'root', 'ongoing'),
        node('last', 'root', 'closed'),
      ],
    });
    for (const key of ['first', 'middle']) {
      const guides = screen.getByTestId(`activity-node-${key}`).querySelector('.activity-tree-guides')!;
      expect(guides.querySelector('[data-guide-role="parent-trunk-before"]')).toBeTruthy();
      expect(guides.querySelector('[data-guide-role="parent-trunk-after"]')).toBeTruthy();
      expect(guides.querySelector('[data-guide-role="branch-connector"]')).toBeTruthy();
    }
    const lastGuides = screen.getByTestId('activity-node-last').querySelector('.activity-tree-guides')!;
    expect(lastGuides.querySelector('[data-guide-role="parent-trunk-before"]')).toBeTruthy();
    expect(lastGuides.querySelector('[data-guide-role="parent-trunk-after"]')).toBeNull();
  });

  it('retains ancestor continuation beside a deep branch without overshooting its last child', () => {
    render(ActivityTree, {
      nodes: [
        node('root', null, 'closed'),
        node('parent', 'root', 'closed'),
        node('root-sibling', 'root', 'closed'),
        node('deep-last', 'parent', 'ongoing'),
      ],
    });
    const parentGuides = screen.getByTestId('activity-node-parent').querySelector('.activity-tree-guides')!;
    const deepGuides = screen.getByTestId('activity-node-deep-last').querySelector('.activity-tree-guides')!;
    expect(parentGuides.querySelector('[data-guide-role="child-trunk"]')).toBeTruthy();
    expect(deepGuides.querySelector('[data-guide-role="ancestor-continuation"]')).toBeTruthy();
    expect(deepGuides.querySelector('[data-guide-role="parent-trunk-before"]')).toBeTruthy();
    expect(deepGuides.querySelector('[data-guide-role="parent-trunk-after"]')).toBeNull();
    expect(deepGuides.querySelector('[data-guide-role="branch-connector"]')).toBeTruthy();
  });

  it('starts collapsed when nothing runs and does not auto-expand a focused closed session', async () => {
    const root = node('root', null, 'closed');
    const child = node('child', 'root', 'closed');
    render(ActivityTree, { nodes: [root, child], focusedSessionId: child.session_id });
    expect(screen.queryByTestId('activity-node-child')).toBeNull();
    expect(screen.getByRole('button', { name: 'Expand root' })).toHaveAttribute('aria-expanded', 'false');
  });

  it('opens only running ancestors and collapses auto-only paths when running ends', async () => {
    const root = node('root', null, 'closed');
    const parent = node('parent', 'root', 'closed');
    const running = node('running', 'parent', 'ongoing');
    const closedChild = node('closed-child', 'running', 'closed');
    const { rerender } = render(ActivityTree, { nodes: [root, parent, running, closedChild] });
    expect(screen.getByTestId('activity-node-running')).toBeTruthy();
    expect(screen.queryByTestId('activity-node-closed-child')).toBeNull();

    await rerender({ nodes: [root, parent, { ...running, activity_state: 'closed' }, closedChild] });
    expect(screen.queryByTestId('activity-node-parent')).toBeNull();
  });

  it('keeps manual expansion when running ends and collapsed forces all branches closed', async () => {
    const root = node('root', null, 'closed');
    const child = node('child', 'root', 'closed');
    const { rerender } = render(ActivityTree, { nodes: [root, child] });
    await fireEvent.click(screen.getByRole('button', { name: 'Expand root' }));
    expect(screen.getByTestId('activity-node-child')).toBeTruthy();
    await rerender({ nodes: [root, child], collapsed: true });
    expect(screen.queryByTestId('activity-node-child')).toBeNull();
    await rerender({ nodes: [root, child], collapsed: false });
    expect(screen.getByTestId('activity-node-child')).toBeTruthy();
  });

  it.each([
    ['ongoing', 'Running', true],
    ['active', 'Active', false],
    ['closed', 'Closed', false],
  ] as const)('maps %s to %s with running orbit=%s', (state, label, orbit) => {
    render(ActivityTree, { nodes: [node(`root-${state}`, null, state)] });
    expect(screen.getByText(label)).toBeTruthy();
    expect(Boolean(screen.queryByTestId('activity-avatar-orbit'))).toBe(orbit);
  });

  it.each([
    ['failed', 'Failed'],
    ['cancelled', 'Cancelled'],
    ['idle', 'Closed'],
  ])('normalizes raw %s status to %s', (status, label) => {
    render(ActivityTree, { nodes: [{ ...node(`root-${status}`, null, 'closed'), status }] });
    expect(screen.getByText(label)).toBeTruthy();
    expect(screen.queryByText('idle')).toBeNull();
  });

  it('lets aggregate ongoing override a failed canonical status', () => {
    render(ActivityTree, {
      nodes: [{ ...node('root-conflict', null, 'ongoing'), status: 'failed' }],
    });
    expect(screen.getByText('Running')).toBeTruthy();
    expect(screen.queryByText('Failed')).toBeNull();
    expect(screen.getByTestId('activity-avatar-orbit')).toBeTruthy();
  });

  it('labels only key equal to root_key as main when parents are missing', () => {
    render(ActivityTree, {
      nodes: [
        node('root', null, 'closed'),
        { ...node('managed-disconnected', null, 'closed'), root_key: 'root', kind: 'managed_agent' as const },
        { ...node('delegate-disconnected', null, 'closed'), root_key: 'root', kind: 'delegate' as const },
      ],
    });
    expect(screen.getAllByText('main')).toHaveLength(1);
    expect(screen.getByText('managed_agent')).toBeTruthy();
    expect(screen.getByText('delegate')).toBeTruthy();
  });

  it('keeps root, ancestors, descendants, and siblings while highlighting only the delegate', async () => {
    const managed = { ...node('managed', 'root', 'closed'), kind: 'managed_agent' as const };
    const delegate = { ...node('delegate', 'managed', 'closed'), kind: 'delegate' as const };
    const sibling = { ...node('sibling', 'root', 'closed'), kind: 'managed_agent' as const };
    render(ActivityTree, {
      nodes: [node('root', null, 'closed'), managed, delegate, sibling],
      focusedSessionId: 'session-delegate',
    });
    await fireEvent.click(screen.getByRole('button', { name: 'Expand root' }));
    await fireEvent.click(screen.getByRole('button', { name: 'Expand managed' }));
    expect(screen.getByTestId('activity-node-root')).toBeTruthy();
    expect(screen.getByTestId('activity-node-managed')).toBeTruthy();
    expect(screen.getByTestId('activity-node-delegate')).toBeTruthy();
    expect(screen.getByTestId('activity-node-sibling')).toBeTruthy();
    expect(screen.getByTestId('activity-node-delegate').firstElementChild).toHaveClass('border-sky-400/60');
    expect(screen.getByTestId('activity-node-delegate').firstElementChild).not.toHaveClass('ring-1');
    expect(screen.getByTestId('activity-node-managed').firstElementChild).toHaveClass('border-transparent');
    expect(screen.queryByText(/Main is the top session/)).toBeNull();
  });

  it('does not render a Work button for zero file changes', () => {
    render(ActivityTree, { nodes: [node('root', null, 'active', 0)] });
    expect(screen.queryByRole('button', { name: 'View Work for root' })).toBeNull();
  });

  it('hides read-only leaves but keeps root, focused sessions, and required ancestors', async () => {
    const root = node('root', null, 'closed', 0);
    const ancestor = node('ancestor', 'root', 'closed', 0);
    const productive = { ...node('productive', 'ancestor', 'closed', 0), summary: { changed_files: 0, commands: 1, mutations: 1, artifacts: 0 } };
    const focused = node('focused', 'root', 'closed', 0);
    const readonly = node('readonly', 'root', 'closed', 0);
    render(ActivityTree, {
      nodes: [root, ancestor, productive, focused, readonly],
      focusedSessionId: focused.session_id,
    });
    await fireEvent.click(screen.getByRole('button', { name: 'Expand root' }));
    await fireEvent.click(screen.getByRole('checkbox', { name: 'Hide read-only' }));
    expect(screen.getByTestId('activity-node-root')).toBeTruthy();
    expect(screen.getByTestId('activity-node-ancestor')).toBeTruthy();
    await fireEvent.click(screen.getByRole('button', { name: 'Expand ancestor' }));
    expect(screen.getByTestId('activity-node-productive')).toBeTruthy();
    expect(screen.getByTestId('activity-node-focused')).toBeTruthy();
    expect(screen.queryByTestId('activity-node-readonly')).toBeNull();
    expect(localStorage.getItem('cognis:activity-tree:hide-read-only:v1')).toBe('true');
  });

  it('sorts siblings by updated time, created time, then ordinal', async () => {
    const root = node('root', null, 'closed');
    const older = { ...node('older', 'root', 'closed'), updated_at: '2026-01-01T00:00:00Z' };
    const newer = { ...node('newer', 'root', 'closed'), updated_at: '2026-01-02T00:00:00Z' };
    render(ActivityTree, { nodes: [root, older, newer], focusedSessionId: newer.session_id });
    await fireEvent.click(screen.getByRole('button', { name: 'Expand root' }));
    const labels = [...screen.getByTestId('activity-tree').querySelectorAll('button[aria-label^="View session"]')]
      .map((element) => element.textContent);
    expect(labels).toEqual(['root', 'newer', 'older']);
  });

  it('hides a command-only internal subtree when no descendant has durable output', async () => {
    const root = node('root', null, 'closed', 0);
    const parent = node('readonly-parent', 'root', 'closed', 0);
    const child = node('readonly-child', 'readonly-parent', 'closed', 0);
    render(ActivityTree, { nodes: [root, parent, child] });
    await fireEvent.click(screen.getByRole('checkbox', { name: 'Hide read-only' }));
    expect(screen.getByTestId('activity-node-root')).toBeTruthy();
    expect(screen.queryByTestId('activity-node-readonly-parent')).toBeNull();
    expect(screen.queryByTestId('activity-node-readonly-child')).toBeNull();
  });

  it('keeps an overlay drawer and Work tab synchronized through A to B to Back', async () => {
    let state: InspectorTraversalState = {
      drawerOpen: true,
      activeTab: 'work',
      presentation: 'overlay',
      focusedSessionId: 'session-a',
      middleSessionId: 'session-a',
      workSessionId: 'session-a',
    };
    const root = node('root', null, 'ongoing');
    const sessionA = node('a', 'root', 'closed');
    const sessionB = node('b', 'root', 'closed');
    render(ActivityTree, {
      nodes: [root, sessionA, sessionB],
      focusedSessionId: 'session-a',
      onViewSession: (sessionId: string) => {
        state = traverseInspectorSession(state, sessionId);
      },
    });
    await fireEvent.click(screen.getByRole('button', { name: 'Expand root' }));
    await fireEvent.click(screen.getByRole('button', { name: 'View session b' }));
    expect(state).toMatchObject({
      drawerOpen: true,
      activeTab: 'work',
      presentation: 'overlay',
      focusedSessionId: 'session-b',
      middleSessionId: 'session-b',
      workSessionId: 'session-b',
    });
    state = traverseInspectorSession(state, 'session-a');
    expect(state).toMatchObject({
      drawerOpen: true,
      activeTab: 'work',
      presentation: 'overlay',
      focusedSessionId: 'session-a',
      middleSessionId: 'session-a',
      workSessionId: 'session-a',
    });
  });
});
