<script lang="ts">
  import ChevronDown from 'lucide-svelte/icons/chevron-down';
  import ChevronRight from 'lucide-svelte/icons/chevron-right';
  import ExternalLink from 'lucide-svelte/icons/external-link';
  import type { WorkCategory, WorkstreamRef } from '$lib/chat-v2/types';
  import {
    compareWorkstreamActivity,
    automaticExpandedWorkstreamKeys,
    focusedAncestorWorkstreamKeys,
    hasFileChanges,
    HIDE_CLOSED_STORAGE_KEY,
    HIDE_READ_ONLY_STORAGE_KEY,
    nodeMatchesFocus,
    visibleWorkstreamKeys,
  } from '$lib/activityTreeState';
  import DiffStat from './DiffStat.svelte';
  import ActivityAvatar from './ActivityAvatar.svelte';
  import WorkstreamIdentityPopover from './WorkstreamIdentityPopover.svelte';
  import WorkstreamTodoProgress from './WorkstreamTodoProgress.svelte';
  import { displaySessionStatus, isEffectiveSessionRunning } from '$lib/session-status';
  import WorkstreamExecutionStatus from './WorkstreamExecutionStatus.svelte';
  type AgentMeta = { agent_id: string; display_name?: string | null; name?: string | null; avatar_url?: string | null };
  type GuideContinuations = boolean[];
  let { nodes = [], agents = [], focusedSessionId = null, runtimeActiveSessionIds = [], collapsed = false, onViewWork, onViewSession }: {
    nodes?: WorkstreamRef[];
    agents?: AgentMeta[];
    focusedSessionId?: string | null;
    runtimeActiveSessionIds?: string[];
    collapsed?: boolean;
    onViewWork?: (sessionId: string, category?: WorkCategory) => void;
    onViewSession?: (sessionId: string, node?: WorkstreamRef) => void;
  } = $props();
  function agentMeta(node: WorkstreamRef): AgentMeta | undefined {
    return agents.find((agent) => agent.agent_id === node.agent_id);
  }
  function agentName(node: WorkstreamRef): string {
    const agent = agentMeta(node);
    return node.agent_display_name ?? agent?.display_name ?? agent?.name ?? node.agent_id;
  }
  function avatarUrl(node: WorkstreamRef): string | null {
    return node.agent_avatar_url ?? agentMeta(node)?.avatar_url ?? null;
  }
  function displayStatus(node: WorkstreamRef): string {
    return displaySessionStatus(
      node.status,
      node.activity_state,
      runtimeActiveSessionIds.includes(node.session_id),
      node.execution_state,
    );
  }
  function nodeIsActive(node: WorkstreamRef): boolean {
    return isEffectiveSessionRunning(
      node.status,
      node.activity_state,
      runtimeActiveSessionIds.includes(node.session_id),
      node.execution_state,
    );
  }
  let expanded = $state<Set<string>>(new Set());
  let manuallyCollapsed = $state<Set<string>>(new Set());
  let hideReadOnly = $state(false);
  let hideClosed = $state(false);
  const automaticExpanded = $derived(automaticExpandedWorkstreamKeys(nodes, runtimeActiveSessionIds));
  const focusedAncestors = $derived(focusedAncestorWorkstreamKeys(nodes, focusedSessionId));
  const visibleKeys = $derived(visibleWorkstreamKeys(nodes, focusedSessionId, {
    hideReadOnly,
    hideClosed,
    runtimeActiveSessionIds,
  }));
  const children = $derived.by(() => {
    const result = new Map<string | null, WorkstreamRef[]>();
    for (const node of nodes) {
      if (!visibleKeys.has(node.key)) continue;
      const parent = node.parent_key ?? null;
      result.set(parent, [...(result.get(parent) ?? []), node].sort(compareWorkstreamActivity));
    }
    return result;
  });
  $effect(() => {
    if (typeof localStorage === 'undefined') return;
    hideReadOnly = localStorage.getItem(HIDE_READ_ONLY_STORAGE_KEY) === 'true';
    hideClosed = localStorage.getItem(HIDE_CLOSED_STORAGE_KEY) === 'true';
  });
  function isOpen(key: string): boolean {
    // An ancestor path to an active descendant must stay open even if the
    // user manually collapsed it earlier; the manual preference is retained
    // in `manuallyCollapsed` and takes effect again once no active
    // descendant remains (i.e. once `automaticExpanded` no longer includes
    // this key).
    if (automaticExpanded.has(key) || focusedAncestors.has(key)) return true;
    return !collapsed && !manuallyCollapsed.has(key) && expanded.has(key);
  }
  function toggle(key: string): void {
    const next = new Set(expanded);
    const nextCollapsed = new Set(manuallyCollapsed);
    if (isOpen(key)) {
      next.delete(key);
      nextCollapsed.add(key);
    } else {
      next.add(key);
      nextCollapsed.delete(key);
    }
    expanded = next;
    manuallyCollapsed = nextCollapsed;
  }
  function toggleReadOnly(): void {
    hideReadOnly = !hideReadOnly;
    if (typeof localStorage !== 'undefined') {
      localStorage.setItem(HIDE_READ_ONLY_STORAGE_KEY, String(hideReadOnly));
    }
  }
  function toggleClosed(): void {
    hideClosed = !hideClosed;
    if (typeof localStorage !== 'undefined') {
      localStorage.setItem(HIDE_CLOSED_STORAGE_KEY, String(hideClosed));
    }
  }
  /**
   * Scrolls the focused/selected row into view once it mounts or becomes
   * focused (e.g. after topology refresh reveals it), without moving
   * keyboard focus so it never interrupts what the user is doing.
   */
  function scrollIntoViewWhenFocused(node: HTMLElement, isFocused: boolean) {
    let announced = false;
    const run = (value: boolean): void => {
      if (!value || announced || typeof node.scrollIntoView !== 'function') return;
      announced = true;
      node.scrollIntoView({ block: 'nearest' });
    };
    run(isFocused);
    return {
      update(value: boolean) {
        if (!value) announced = false;
        run(value);
      },
    };
  }
</script>
{#snippet branch(parent: string | null, depth: number, ancestorContinuations: GuideContinuations)}
  {#each (children.get(parent) ?? []) as node, index (node.key)}
    {@const descendants = children.get(node.key) ?? []}
    {@const open = isOpen(node.key)}
    {@const summary = node.summary}
    {@const running = displayStatus(node) === 'Running'}
    {@const lastChild = index === (children.get(parent)?.length ?? 0) - 1}
    {@const focused = nodeMatchesFocus(node, focusedSessionId)}
    <li class="min-w-0" data-testid={`activity-node-${node.key}`}>
      <div
        class={`activity-tree-node relative min-w-0 rounded-lg border px-2 py-2 text-xs ${focused ? 'border-sky-400/60 bg-sky-500/10 text-sky-100 shadow-[inset_0_0_0_1px_rgb(56_189_248_/_0.12)]' : 'border-transparent text-slate-400'}`}
        class:activity-tree-root={depth === 0}
        style={`--tree-depth:${depth}`}
        data-testid="activity-tree-row"
        use:scrollIntoViewWhenFocused={focused}
      >
        <div class="activity-tree-guides" aria-hidden="true">
          {#each ancestorContinuations as continues, guideDepth}
            {#if continues}<span class="activity-tree-guide" data-guide-role="ancestor-continuation" style={`--guide-depth:${guideDepth}`}></span>{/if}
          {/each}
          {#if depth > 0}
            <span class="activity-tree-parent-trunk activity-tree-parent-trunk-before" data-guide-role="parent-trunk-before"></span>
            {#if !lastChild}<span class="activity-tree-parent-trunk activity-tree-parent-trunk-after" data-guide-role="parent-trunk-after"></span>{/if}
            <span class="activity-tree-branch" data-guide-role="branch-connector"></span>
          {/if}
          {#if open && descendants.length}<span class="activity-tree-child-trunk" data-guide-role="child-trunk"></span>{/if}
        </div>
        <div class="activity-tree-content min-w-0">
          <div class="activity-tree-title-row flex min-w-0 items-center gap-1.5">
            {#if descendants.length}
              <button class="activity-tree-caret shrink-0" type="button" aria-label={`${open ? 'Collapse' : 'Expand'} ${node.title}`} aria-expanded={open} onclick={() => toggle(node.key)}>
                {#if open}<ChevronDown class="h-3.5 w-3.5" />{:else}<ChevronRight class="h-3.5 w-3.5" />{/if}
              </button>
            {:else}<span class="activity-tree-caret block shrink-0" aria-hidden="true"></span>{/if}
            <button type="button" class="scrollbar-hidden-x block min-w-0 flex-1 text-left font-medium text-slate-100" onclick={() => onViewSession?.(node.session_id, node)} aria-label={`View session ${node.title}`}>{node.title}</button>
            {#if node.key !== node.root_key && (node.kind === 'managed' || node.kind === 'managed_agent') && node.conversation_id}
              <a
                class="inline-flex h-6 w-6 shrink-0 items-center justify-center rounded text-slate-500 transition hover:bg-slate-800 hover:text-sky-300 focus:outline-none focus-visible:ring-2 focus-visible:ring-sky-400/60"
                href={`/chat/${encodeURIComponent(node.conversation_id)}`}
                target="_blank"
                rel="noopener noreferrer"
                aria-label={`Open ${node.title} in new window`}
                onclick={(event) => event.stopPropagation()}
              ><ExternalLink class="h-3.5 w-3.5" /></a>
            {/if}
          </div>
          <div class="activity-tree-metadata-row mt-1 flex min-w-0 items-center gap-1.5">
            <span class="activity-tree-caret shrink-0" aria-hidden="true"></span>
            <WorkstreamIdentityPopover node={node} name={agentName(node)} avatarUrl={avatarUrl(node)} {running} />
            <WorkstreamExecutionStatus label={displayStatus(node)} active={nodeIsActive(node)} />
            <div class="activity-tree-trailing ml-auto flex shrink-0 items-center gap-1.5">
              {#if node.todo_progress && node.todo_progress.total > 0}
                <WorkstreamTodoProgress progress={node.todo_progress} />
              {/if}
              {#if summary && hasFileChanges(node)}
                <button type="button" class="rounded border border-slate-700 px-1.5 py-0.5 text-sky-200" onclick={() => onViewWork?.(node.session_id, 'files')} aria-label={`View Work for ${node.title}`}><DiffStat files={summary.changed_files} additions={summary.additions} deletions={summary.deletions} compact /></button>
              {/if}
            </div>
          </div>
        </div>
      </div>
      {#if open && descendants.length}<ul>{@render branch(
        node.key,
        depth + 1,
        depth === 0
          ? ancestorContinuations
          : [...ancestorContinuations, index < (children.get(parent)?.length ?? 0) - 1],
      )}</ul>{/if}
    </li>
  {/each}
{/snippet}
<div class="mb-2 flex justify-end gap-3">
  <label class="inline-flex cursor-pointer items-center gap-2 text-xs text-slate-400">
    <input type="checkbox" class="h-4 w-4 rounded border-slate-600 bg-slate-900 text-sky-400" checked={hideClosed} onchange={toggleClosed} data-testid="activity-tree-hide-closed" />
    Hide closed
  </label>
  <label class="inline-flex cursor-pointer items-center gap-2 text-xs text-slate-400">
    <input type="checkbox" class="h-4 w-4 rounded border-slate-600 bg-slate-900 text-sky-400" checked={hideReadOnly} onchange={toggleReadOnly} data-testid="activity-tree-hide-read-only" />
    Hide read-only
  </label>
</div>
<div class="max-w-full overflow-x-auto [container-type:inline-size]" data-testid="activity-tree"><ul class="min-w-[20rem] space-y-1">{@render branch(null, 0, [])}</ul></div>

<style>
  .activity-tree-node {
    padding-left: calc(0.5rem + var(--tree-depth) * 9px);
    isolation: isolate;
  }
  .activity-tree-caret {
    width: 0.875rem;
    height: 0.875rem;
  }
  .activity-tree-guides {
    position: absolute;
    inset: 0;
    z-index: -1;
    pointer-events: none;
  }
  .activity-tree-guide {
    position: absolute;
    top: -0.25rem;
    bottom: -0.25rem;
    left: calc(0.94rem + var(--guide-depth) * 9px);
    border-left: 1px solid rgb(71 85 105 / 0.45);
  }
  .activity-tree-branch {
    position: absolute;
    top: 1.05rem;
    left: calc(0.94rem + (var(--tree-depth) - 1) * 9px);
    width: 9px;
    border-top: 1px solid rgb(71 85 105 / 0.55);
  }
  .activity-tree-parent-trunk,
  .activity-tree-child-trunk {
    position: absolute;
    left: calc(0.94rem + (var(--tree-depth) - 1) * 9px);
    border-left: 1px solid rgb(71 85 105 / 0.55);
  }
  .activity-tree-parent-trunk-before {
    top: -0.25rem;
    height: calc(1.05rem + 0.25rem);
  }
  .activity-tree-parent-trunk-after {
    top: 1.05rem;
    bottom: -0.25rem;
  }
  .activity-tree-child-trunk {
    top: 1.05rem;
    bottom: -0.25rem;
    left: calc(0.94rem + var(--tree-depth) * 9px);
  }
  .activity-tree-root .activity-tree-branch {
    display: none;
  }
</style>
