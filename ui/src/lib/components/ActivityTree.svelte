<script lang="ts">
  import ChevronDown from 'lucide-svelte/icons/chevron-down';
  import ChevronRight from 'lucide-svelte/icons/chevron-right';
  import type { WorkCategory, WorkstreamRef } from '$lib/chat-v2/types';
  import {
    compareWorkstreamActivity,
    automaticExpandedWorkstreamKeys,
    hasFileChanges,
    HIDE_READ_ONLY_STORAGE_KEY,
    visibleWorkstreamKeys,
  } from '$lib/activityTreeState';
  import DiffStat from './DiffStat.svelte';
  import ActivityAvatar from './ActivityAvatar.svelte';
  type AgentMeta = { agent_id: string; display_name?: string | null; name?: string | null; avatar_url?: string | null };
  type GuideContinuations = boolean[];
  let { nodes = [], agents = [], focusedSessionId = null, collapsed = false, onViewWork, onViewSession }: {
    nodes?: WorkstreamRef[];
    agents?: AgentMeta[];
    focusedSessionId?: string | null;
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
    if (node.activity_state === 'ongoing') return 'Running';
    if (node.activity_state === 'active') return 'Active';
    if (node.status === 'failed') return 'Failed';
    if (node.status === 'cancelled') return 'Cancelled';
    return 'Closed';
  }
  let expanded = $state<Set<string>>(new Set());
  let manuallyCollapsed = $state<Set<string>>(new Set());
  let hideReadOnly = $state(false);
  const automaticExpanded = $derived(automaticExpandedWorkstreamKeys(nodes));
  const visibleKeys = $derived(visibleWorkstreamKeys(nodes, focusedSessionId, hideReadOnly));
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
  });
  function isOpen(key: string): boolean {
    return !collapsed && !manuallyCollapsed.has(key) && (expanded.has(key) || automaticExpanded.has(key));
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
</script>
{#snippet branch(parent: string | null, depth: number, ancestorContinuations: GuideContinuations)}
  {#each (children.get(parent) ?? []) as node, index (node.key)}
    {@const descendants = children.get(node.key) ?? []}
    {@const open = isOpen(node.key)}
    {@const summary = node.summary}
    {@const lastChild = index === (children.get(parent)?.length ?? 0) - 1}
    <li class="min-w-0" data-testid={`activity-node-${node.key}`}>
      <div
        class={`activity-tree-node relative min-w-0 rounded-lg border px-2 py-2 text-xs ${node.session_id === focusedSessionId ? 'border-sky-400/60 bg-sky-500/10 text-sky-100 shadow-[inset_0_0_0_1px_rgb(56_189_248_/_0.12)]' : 'border-transparent text-slate-400'}`}
        class:activity-tree-root={depth === 0}
        style={`--tree-depth:${depth}`}
        data-testid="activity-tree-row"
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
          </div>
          <div class="activity-tree-metadata-row mt-1 flex min-w-0 items-center gap-1.5">
            <span class="activity-tree-caret shrink-0" aria-hidden="true"></span>
            <ActivityAvatar name={agentName(node)} avatarUrl={avatarUrl(node)} turnInProgress={node.activity_state === 'ongoing'} class="h-6 w-6 shrink-0" />
            <span class="scrollbar-hidden-x min-w-0 text-slate-200">{agentName(node)}</span>
            {#if node.agent_profile_id}<span class="scrollbar-hidden-x min-w-0">· {node.agent_profile_id}</span>{/if}
            <span class="shrink-0">{displayStatus(node)}</span>
            <div class="activity-tree-trailing ml-auto flex shrink-0 items-center gap-1.5">
              <span class="rounded bg-slate-800 px-1.5 py-0.5 text-[10px]">{node.key === node.root_key ? 'main' : node.kind}</span>
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
<div class="mb-2 flex justify-end">
  <label class="inline-flex cursor-pointer items-center gap-2 text-xs text-slate-400">
    <input type="checkbox" class="h-4 w-4 rounded border-slate-600 bg-slate-900 text-sky-400" checked={hideReadOnly} onchange={toggleReadOnly} />
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
