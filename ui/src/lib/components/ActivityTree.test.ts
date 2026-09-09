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
  it('keeps a running rotated child visible under an idle parent, then hides it after settlement', async () => {
    const root = { ...node('root', null, 'active'), execution_state: 'idle' as const };
    const child = {
      ...node('child', 'root', 'closed'),
      status: 'completed',
      backing_session_ids: ['session-child-old', 'session-child'],
      execution_state: 'running' as const,
    };
    const { rerender } = render(ActivityTree, { nodes: [root, child] });
    await fireEvent.click(screen.getByRole('checkbox', { name: 'Hide closed' }));
    expect(screen.getByRole('checkbox', { name: 'Hide closed' })).toBeChecked();
    expect(screen.getByTestId('activity-node-child')).toHaveTextContent('Running');
    const parentRow = screen.getByTestId('activity-node-root').firstElementChild!;
    expect(parentRow).toHaveTextContent('Idle');
    expect(parentRow.querySelector('[data-testid="activity-avatar-orbit"]')).toBeNull();
    await rerender({
      nodes: [root, { ...child, execution_state: 'completed' }],
      runtimeActiveSessionIds: ['session-child'],
    });
    expect(screen.queryByTestId('activity-node-child')).toBeNull();
    expect(screen.getByText('Idle')).toBeTruthy();
  });

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
    expect(screen.queryByText('Agent Display')).toBeNull();
    expect(screen.queryByText(/developer-senior/)).toBeNull();
    await fireEvent.pointerDown(screen.getAllByRole('button', { name: 'Session identity: Agent Display' })[1]);
    const identityTooltip = screen.getByRole('tooltip');
    expect(identityTooltip).toHaveTextContent('Agent Display');
    expect(identityTooltip).toHaveTextContent('Profile: developer-senior');
    expect(identityTooltip).toHaveTextContent('Kind: Managed');
    expect(identityTooltip.parentElement).toBe(document.body);
    expect(identityTooltip).toHaveClass('z-[2147483647]', 'whitespace-pre-line', 'bg-slate-950');
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

  it('opens managed child conversations in a new window without selecting the row', async () => {
    const onViewSession = vi.fn();
    const managed = {
      ...node('managed-link', 'root', 'ongoing'),
      kind: 'managed',
      conversation_id: 'managed/conversation',
    };
    render(ActivityTree, {
      nodes: [node('root', null, 'active'), managed],
      onViewSession,
    });

    const link = screen.getByRole('link', { name: 'Open managed-link in new window' });
    expect(link).toHaveAttribute('href', '/chat/managed%2Fconversation');
    expect(link).toHaveAttribute('target', '_blank');
    expect(link).toHaveAttribute('rel', 'noopener noreferrer');
    await fireEvent.click(link);
    expect(onViewSession).not.toHaveBeenCalled();
    expect(screen.queryByRole('link', { name: 'Open root in new window' })).toBeNull();
  });

  it('does not render a new-window link for a delegate carrying the controller conversation ID', () => {
    render(ActivityTree, {
      nodes: [
        node('root', null, 'active'),
        {
          ...node('delegate-only', 'root', 'ongoing'),
          kind: 'delegate',
          conversation_id: 'controller-conversation',
        },
      ],
    });
    expect(screen.queryByRole('link', { name: 'Open delegate-only in new window' })).toBeNull();
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

  it('starts collapsed when nothing runs and no session is focused', async () => {
    const root = node('root', null, 'closed');
    const child = node('child', 'root', 'closed');
    render(ActivityTree, { nodes: [root, child] });
    expect(screen.queryByTestId('activity-node-child')).toBeNull();
    expect(screen.getByRole('button', { name: 'Expand root' })).toHaveAttribute('aria-expanded', 'false');
  });

  it('force-expands the ancestor of a focused closed session so the selected node is reachable', async () => {
    const root = node('root', null, 'closed');
    const child = node('child', 'root', 'closed');
    render(ActivityTree, { nodes: [root, child], focusedSessionId: child.session_id });
    expect(screen.getByTestId('activity-node-child')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Collapse root' })).toHaveAttribute('aria-expanded', 'true');
  });

  it('resolves focus and force-expands ancestors when the focused session is a backing (rotated) session id', () => {
    const root = node('root', null, 'closed');
    const rotated = { ...node('rotated', 'root', 'closed'), backing_session_ids: ['session-old-rotation'] };
    render(ActivityTree, { nodes: [root, rotated], focusedSessionId: 'session-old-rotation' });
    expect(screen.getByTestId('activity-node-rotated')).toBeTruthy();
    expect(screen.getByTestId('activity-node-rotated').firstElementChild).toHaveClass('border-sky-400/60');
    expect(screen.getByRole('button', { name: 'Collapse root' })).toHaveAttribute('aria-expanded', 'true');
  });

  it('restores the manual collapse preference once the focused session changes away', async () => {
    const root = node('root', null, 'closed');
    const child = node('child', 'root', 'closed');
    const { rerender } = render(ActivityTree, { nodes: [root, child], focusedSessionId: child.session_id });
    expect(screen.getByTestId('activity-node-child')).toBeTruthy();
    await fireEvent.click(screen.getByRole('button', { name: 'Collapse root' }));
    // Manual collapse cannot hide the currently focused descendant.
    expect(screen.getByTestId('activity-node-child')).toBeTruthy();
    await rerender({ nodes: [root, child], focusedSessionId: null });
    expect(screen.queryByTestId('activity-node-child')).toBeNull();
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

  it('keeps terminal status authoritative over aggregate and runtime activity', () => {
    render(ActivityTree, {
      nodes: [{ ...node('root-conflict', null, 'ongoing'), status: 'failed' }],
      runtimeActiveSessionIds: ['session-root-conflict'],
    });
    expect(screen.getByText('Failed')).toBeTruthy();
    expect(screen.queryByText('Running')).toBeNull();
    expect(screen.queryByTestId('activity-avatar-orbit')).toBeNull();
  });

  it('treats terminated as terminal despite runtime activity and filters its read-only delegate', async () => {
    const root = node('root', null, 'active', 0);
    const terminated = {
      ...node('terminated', 'root', 'ongoing', 0),
      kind: 'delegate',
      agent_id: 'system:research',
      status: 'terminated',
    };
    const { rerender } = render(ActivityTree, {
      nodes: [root, terminated],
      runtimeActiveSessionIds: [terminated.session_id],
      focusedSessionId: terminated.session_id,
    });
    // root auto-expands because the focused session is one of its descendants.
    expect(screen.getByTestId('activity-node-terminated')).toHaveTextContent('Closed');
    expect(screen.getByTestId('activity-node-terminated').querySelector('[data-testid="activity-avatar-orbit"]')).toBeNull();
    await fireEvent.click(screen.getByRole('checkbox', { name: 'Hide read-only' }));
    expect(screen.getByTestId('activity-node-terminated')).toBeTruthy();

    await rerender({
      nodes: [root, terminated],
      runtimeActiveSessionIds: [terminated.session_id],
      focusedSessionId: null,
    });
    expect(screen.queryByTestId('activity-node-terminated')).toBeNull();
  });

  it('keeps kind labels out of rows and exposes them in identity popovers', async () => {
    render(ActivityTree, {
      nodes: [
        node('root', null, 'closed'),
        { ...node('managed-disconnected', null, 'closed'), root_key: 'root', kind: 'managed_agent' as const },
        { ...node('delegate-disconnected', null, 'closed'), root_key: 'root', kind: 'delegate' as const },
      ],
    });
    expect(screen.queryByText('main')).toBeNull();
    expect(screen.queryByText('managed_agent')).toBeNull();
    expect(screen.queryByText('delegate')).toBeNull();
    await fireEvent.pointerDown(screen.getAllByRole('button', { name: 'Session identity: agent' })[0]);
    expect(screen.getByRole('tooltip')).toHaveTextContent('Kind: Main');
  });

  it('uses nonterminal runtime activity for Running and orbit', () => {
    render(ActivityTree, {
      nodes: [node('runtime', null, 'active')],
      runtimeActiveSessionIds: ['session-runtime'],
    });
    expect(screen.getByText('Running')).toBeTruthy();
    expect(screen.getByTestId('activity-avatar-orbit')).toBeTruthy();
  });

  it('renders weighted todo progress before file statistics and omits zero totals', async () => {
    const withTodos = {
      ...node('todo', null, 'active'),
      todo_progress: { total: 4, completed: 1, in_progress: 2 },
    };
    const { rerender } = render(ActivityTree, { nodes: [withTodos] });
    const progress = screen.getByTestId('workstream-todo-progress');
    const files = screen.getByRole('button', { name: 'View Work for todo' });
    expect(progress).toHaveAttribute('data-progress', '0.5');
    expect(progress).toHaveAccessibleName('Todo progress: 1 completed, 2 in progress, 4 total');
    expect(progress.compareDocumentPosition(files) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    await rerender({ nodes: [{ ...withTodos, todo_progress: { total: 0, completed: 0, in_progress: 0 } }] });
    expect(screen.queryByTestId('workstream-todo-progress')).toBeNull();
  });

  it('keeps root, ancestors, descendants, and siblings while highlighting only the delegate', async () => {
    const managed = { ...node('managed', 'root', 'closed'), kind: 'managed_agent' as const };
    const delegate = { ...node('delegate', 'managed', 'closed'), kind: 'delegate' as const };
    const sibling = { ...node('sibling', 'root', 'closed'), kind: 'managed_agent' as const };
    render(ActivityTree, {
      nodes: [node('root', null, 'closed'), managed, delegate, sibling],
      focusedSessionId: 'session-delegate',
    });
    // root and managed auto-expand because they are ancestors of the focused delegate.
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
    // root auto-expands because the focused session is its descendant.
    await fireEvent.click(screen.getByRole('checkbox', { name: 'Hide read-only' }));
    expect(screen.getByTestId('activity-node-root')).toBeTruthy();
    expect(screen.getByTestId('activity-node-ancestor')).toBeTruthy();
    await fireEvent.click(screen.getByRole('button', { name: 'Expand ancestor' }));
    expect(screen.getByTestId('activity-node-productive')).toBeTruthy();
    expect(screen.getByTestId('activity-node-focused')).toBeTruthy();
    expect(screen.queryByTestId('activity-node-readonly')).toBeNull();
    expect(localStorage.getItem('cognis:activity-tree:hide-read-only:v1')).toBe('true');
  });

  it('retains a runtime-active read-only delegate, then filters it immediately when runtime ends', async () => {
    const root = node('root', null, 'active', 0);
    const delegate = {
      ...node('explore', 'root', 'active', 0),
      kind: 'delegate',
      agent_id: 'system:explore',
    };
    const { rerender } = render(ActivityTree, {
      nodes: [root, delegate],
      runtimeActiveSessionIds: [delegate.session_id],
    });
    await fireEvent.click(screen.getByRole('checkbox', { name: 'Hide read-only' }));
    expect(screen.getByTestId('activity-node-explore')).toBeTruthy();
    expect(screen.getByTestId('activity-node-explore').querySelector('[data-testid="activity-avatar-orbit"]')).toBeTruthy();

    await rerender({
      nodes: [root, { ...delegate, activity_state: 'active' }],
      runtimeActiveSessionIds: [],
    });
    expect(screen.queryByTestId('activity-node-explore')).toBeNull();
  });

  it('hides closed leaves but keeps root, focused, active/ongoing, and required ancestors', async () => {
    const root = node('root', null, 'active', 0);
    const closedAncestor = node('closed-ancestor', 'root', 'closed', 0);
    const ongoingChild = node('ongoing-child', 'closed-ancestor', 'ongoing', 0);
    const focusedClosed = node('focused-closed', 'root', 'closed', 0);
    const closedLeaf = node('closed-leaf', 'root', 'closed', 0);
    render(ActivityTree, {
      nodes: [root, closedAncestor, ongoingChild, focusedClosed, closedLeaf],
      focusedSessionId: focusedClosed.session_id,
    });
    // closed-ancestor auto-expands because its descendant is ongoing.
    await fireEvent.click(screen.getByRole('checkbox', { name: 'Hide closed' }));
    expect(screen.getByTestId('activity-node-root')).toBeTruthy();
    // closed-ancestor is itself closed but must stay because it is the required
    // ancestor of the ongoing (never-closed) child.
    expect(screen.getByTestId('activity-node-closed-ancestor')).toBeTruthy();
    expect(screen.getByTestId('activity-node-ongoing-child')).toBeTruthy();
    expect(screen.getByTestId('activity-node-focused-closed')).toBeTruthy();
    expect(screen.queryByTestId('activity-node-closed-leaf')).toBeNull();
    expect(localStorage.getItem('cognis:activity-tree:hide-closed:v1')).toBe('true');
  });

  it('persists the Hide closed preference independently of Hide read-only and composes both filters', async () => {
    localStorage.setItem('cognis:activity-tree:hide-closed:v1', 'true');
    const root = node('root', null, 'active', 0);
    const closedReadOnly = node('closed-read-only', 'root', 'closed', 0);
    const openReadOnly = node('open-read-only', 'root', 'ongoing', 0);
    const closedProductive = { ...node('closed-productive', 'root', 'closed', 0), summary: { changed_files: 3, commands: 1, mutations: 1, artifacts: 0 } };
    render(ActivityTree, {
      nodes: [root, closedReadOnly, openReadOnly, closedProductive],
    });
    // open-read-only is ongoing, so root auto-expands without a manual click.
    // Hide closed loaded from storage: closed-read-only and closed-productive drop out.
    expect(screen.queryByTestId('activity-node-closed-read-only')).toBeNull();
    expect(screen.getByTestId('activity-node-open-read-only')).toBeTruthy();
    expect(screen.queryByTestId('activity-node-closed-productive')).toBeNull();

    await fireEvent.click(screen.getByRole('checkbox', { name: 'Hide read-only' }));
    // Ongoing nodes remain visible even when both filters are enabled.
    expect(screen.getByTestId('activity-node-open-read-only')).toBeTruthy();
    expect(screen.getByTestId('activity-node-root')).toBeTruthy();
  });

  it('sorts siblings by updated time, created time, then ordinal', async () => {
    const root = node('root', null, 'closed');
    const older = { ...node('older', 'root', 'closed'), updated_at: '2026-01-01T00:00:00Z' };
    const newer = { ...node('newer', 'root', 'closed'), updated_at: '2026-01-02T00:00:00Z' };
    render(ActivityTree, { nodes: [root, older, newer], focusedSessionId: newer.session_id });
    // root auto-expands because the focused session is its descendant.
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

  it('forces an ancestor open for an active execution_state descendant even after manual collapse, then restores the manual preference', async () => {
    const root = node('root', null, 'closed');
    const active = { ...node('active-child', 'root', 'closed'), execution_state: 'running' as const };
    const { rerender } = render(ActivityTree, { nodes: [root, active] });
    // root auto-expands because its descendant carries an active execution_state.
    expect(screen.getByTestId('activity-node-active-child')).toBeTruthy();

    await fireEvent.click(screen.getByRole('button', { name: 'Collapse root' }));
    // Manual collapse cannot hide the active descendant.
    expect(screen.getByTestId('activity-node-active-child')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Collapse root' })).toHaveAttribute('aria-expanded', 'true');

    await rerender({ nodes: [root, { ...active, execution_state: 'completed' }] });
    // The manual collapse preference is restored once no active descendant remains.
    expect(screen.queryByTestId('activity-node-active-child')).toBeNull();
    expect(screen.getByRole('button', { name: 'Expand root' })).toHaveAttribute('aria-expanded', 'false');
  });

  it('uses the avatar as the only progress indicator and keeps status badges text-only', () => {
    const cases = [
      ['queued', 'Queued'],
      ['running', 'Running'],
      ['waiting', 'Waiting'],
      ['recovering', 'Recovering'],
    ] as const;
    for (const [state, label] of cases) {
      const { unmount } = render(ActivityTree, {
        nodes: [{ ...node(`root-${state}`, null, 'active'), execution_state: state }],
      });
      const status = screen.getByTestId('workstream-execution-status');
      expect(status).toHaveAttribute('role', 'status');
      expect(status).toHaveAttribute('aria-busy', 'true');
      expect(status).toHaveTextContent(label);
      expect(screen.queryByTestId('workstream-execution-spinner')).toBeNull();
      unmount();
    }
    for (const [state, label] of [['completed', 'Completed'], ['failed', 'Failed'], ['cancelled', 'Cancelled']] as const) {
      const { unmount } = render(ActivityTree, {
        nodes: [{ ...node(`root-terminal-${state}`, null, 'closed'), execution_state: state }],
      });
      const status = screen.getByTestId('workstream-execution-status');
      expect(status).not.toHaveAttribute('role', 'status');
      expect(status).not.toHaveAttribute('aria-busy');
      expect(status).toHaveTextContent(label);
      expect(screen.queryByTestId('workstream-execution-spinner')).toBeNull();
      unmount();
    }
    const { unmount: unmountIdle } = render(ActivityTree, {
      nodes: [{ ...node('root-idle', null, 'active'), execution_state: 'idle' }],
      runtimeActiveSessionIds: ['root-idle'],
    });
    const idleStatus = screen.getByTestId('workstream-execution-status');
    expect(idleStatus).toHaveTextContent('Idle');
    expect(idleStatus).not.toHaveAttribute('aria-busy');
    expect(screen.queryByTestId('workstream-execution-spinner')).toBeNull();
    unmountIdle();
  });

  it('dismisses an overlay drawer while keeping the Work tab and child scope synchronized', async () => {
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
    // root auto-expands because the focused session is its descendant.
    await fireEvent.click(screen.getByRole('button', { name: 'View session b' }));
    expect(state).toMatchObject({
      drawerOpen: false,
      activeTab: 'work',
      presentation: 'closed',
      focusedSessionId: 'session-b',
      middleSessionId: 'session-b',
      workSessionId: 'session-b',
    });
    state = traverseInspectorSession(state, 'session-a');
    expect(state).toMatchObject({
      drawerOpen: false,
      activeTab: 'work',
      presentation: 'closed',
      focusedSessionId: 'session-a',
      middleSessionId: 'session-a',
      workSessionId: 'session-a',
    });
  });
});
