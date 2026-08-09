<script lang="ts">
  import { onDestroy } from 'svelte';
  import ArrowLeft from 'lucide-svelte/icons/arrow-left';
  import ChevronDown from 'lucide-svelte/icons/chevron-down';
  import ChevronRight from 'lucide-svelte/icons/chevron-right';
  import FileCode2 from 'lucide-svelte/icons/file-code-2';
  import Folder from 'lucide-svelte/icons/folder';
  import FolderOpen from 'lucide-svelte/icons/folder-open';
  import PanelLeftClose from 'lucide-svelte/icons/panel-left-close';
  import PanelLeftOpen from 'lucide-svelte/icons/panel-left-open';
  import Search from 'lucide-svelte/icons/search';
  import Minimize2 from 'lucide-svelte/icons/minimize-2';

  import FileDiffViewer from '$lib/components/FileDiffViewer.svelte';
  import {
    buildFileTree,
    commonFileTreeRoot,
    combineFileDiffHistory,
    defaultExpandedFolders,
    fileNodes,
    filterFileTree,
    visibleFileTreeNodes,
    type FileChangeStatus,
    type FileTreeFile,
    type FileTreeNode,
    type WorkFileDiff,
  } from '$lib/work/fileTree';
  import { getWorkFileTreeState, setWorkFileTreeState } from '$lib/work/workFileTreeState';

  let { diffs, cacheKey } = $props<{ diffs: WorkFileDiff[]; cacheKey?: string }>();

  const WIDTH_KEY = 'cognis.work.fileTreeWidth';
  let query = $state('');
  let statusFilter = $state<FileChangeStatus | 'all'>('all');
  let expanded = $state<Set<string>>(new Set());
  let selectedId = $state<string | null>(null);
  let focusedId = $state<string | null>(null);
  let treeWidth = $state(280);
  let treeCollapsed = $state(false);
  let mobileDiffOpen = $state(false);
  let focusedDiffOpen = $state(false);
  let filterInput = $state<HTMLInputElement | null>(null);
  let treeElement = $state<HTMLElement | null>(null);
  let diffElement = $state<HTMLElement | null>(null);
  let explorerElement = $state<HTMLElement | null>(null);
  let explorerWidth = $state(0);
  let initializedSignature = '';
  let explorerObserver: ResizeObserver | null = null;
  let restoredCache = false;

  const tree = $derived(buildFileTree(diffs));
  const commonRoot = $derived(commonFileTreeRoot(diffs));
  const files = $derived(fileNodes(tree));
  const totals = $derived(files.reduce(
    (result, file) => ({
      additions: result.additions + file.counts.additions,
      deletions: result.deletions + file.counts.deletions,
    }),
    { additions: 0, deletions: 0 },
  ));
  const filteredTree = $derived(filterFileTree(
    tree,
    query,
    statusFilter === 'all' ? new Set<FileChangeStatus>() : new Set([statusFilter])
  ));
  const effectiveExpanded = $derived(
    query.trim() || statusFilter !== 'all'
      ? new Set([...expanded, ...defaultExpandedFolders(filteredTree, true)])
      : expanded
  );
  const visible = $derived(visibleFileTreeNodes(filteredTree, effectiveExpanded));
  const selected = $derived(files.find((file) => file.id === selectedId) ?? files[0] ?? null);

  $effect(() => {
    const signature = files.map((file) => file.id).join('\n');
    if (signature === initializedSignature) return;
    initializedSignature = signature;
    const cached = cacheKey ? getWorkFileTreeState(cacheKey) : null;
    if (cached && !restoredCache) {
      restoredCache = true;
      query = cached.query;
      statusFilter = cached.statusFilter as FileChangeStatus | 'all';
      expanded = new Set(cached.expanded);
      selectedId = files.some((file) => file.id === cached.selectedId) ? cached.selectedId : files[0]?.id ?? null;
      focusedId = selectedId;
      requestAnimationFrame(() => {
        if (treeElement) treeElement.scrollTop = cached.treeScrollTop;
        if (diffElement) diffElement.scrollTop = cached.diffScrollTop;
      });
      return;
    }
    selectedId = files.some((file) => file.id === selectedId)
      ? selectedId
      : files[0]?.id ?? null;
    expanded = files.length > 40 ? new Set() : defaultExpandedFolders(tree, true);
    focusedId = selectedId ?? visible[0]?.node.id ?? null;
  });

  $effect(() => {
    if (!explorerElement || typeof ResizeObserver === 'undefined') return;
    explorerObserver?.disconnect();
    explorerObserver = new ResizeObserver(([entry]) => {
      explorerWidth = entry?.contentRect.width ?? 0;
      if (explorerWidth >= 880) {
        mobileDiffOpen = false;
      } else {
        treeCollapsed = false;
      }
    });
    explorerObserver.observe(explorerElement);
    return () => {
      explorerObserver?.disconnect();
      explorerObserver = null;
    };
  });

  $effect(() => {
    if (typeof window === 'undefined') return;
    const stored = Number(window.localStorage.getItem(WIDTH_KEY));
    if (Number.isFinite(stored) && stored >= 200 && stored <= 480) treeWidth = stored;
  });

  function toggleFolder(node: FileTreeNode): void {
    if (node.kind !== 'folder') return;
    const next = new Set(expanded);
    if (next.has(node.id)) next.delete(node.id);
    else next.add(node.id);
    expanded = next;
    focusedId = node.id;
  }

  function selectFile(file: FileTreeFile): void {
    selectedId = file.id;
    focusedId = file.id;
    mobileDiffOpen = explorerWidth < 880;
  }

  function focusNode(id: string): void {
    focusedId = id;
    requestAnimationFrame(() => {
      const match = Array.from(treeElement?.querySelectorAll<HTMLElement>('[data-node-id]') ?? [])
        .find((element) => element.dataset.nodeId === id);
      match?.focus();
    });
  }

  function onTreeKeydown(event: KeyboardEvent): void {
    if (event.key === '/') {
      event.preventDefault();
      filterInput?.focus();
      return;
    }
    const index = Math.max(0, visible.findIndex((item) => item.node.id === focusedId));
    const current = visible[index];
    if (!current) return;
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      const delta = event.key === 'ArrowDown' ? 1 : -1;
      focusNode(visible[Math.max(0, Math.min(visible.length - 1, index + delta))].node.id);
    } else if (event.key === 'ArrowRight' && current.node.kind === 'folder') {
      event.preventDefault();
      if (!expanded.has(current.node.id)) toggleFolder(current.node);
      else if (current.node.children[0]) focusNode(current.node.children[0].id);
    } else if (event.key === 'ArrowLeft') {
      event.preventDefault();
      if (current.node.kind === 'folder' && expanded.has(current.node.id)) toggleFolder(current.node);
      else if (current.parentId) focusNode(current.parentId);
    } else if (event.key === 'Enter') {
      event.preventDefault();
      if (current.node.kind === 'folder') toggleFolder(current.node);
      else selectFile(current.node);
    }
  }

  function setTreeWidth(nextWidth: number): void {
    treeWidth = Math.max(200, Math.min(480, nextWidth));
    window.localStorage.setItem(WIDTH_KEY, String(treeWidth));
  }

  function startResize(event: PointerEvent): void {
    const startX = event.clientX;
    const startWidth = treeWidth;
    const move = (next: PointerEvent) => setTreeWidth(startWidth + next.clientX - startX);
    const stop = () => {
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', stop);
    };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', stop);
  }

  function resizeWithKeyboard(event: KeyboardEvent): void {
    const step = event.shiftKey ? 40 : 10;
    if (event.key === 'ArrowLeft') setTreeWidth(treeWidth - step);
    else if (event.key === 'ArrowRight') setTreeWidth(treeWidth + step);
    else if (event.key === 'Home') setTreeWidth(200);
    else if (event.key === 'End') setTreeWidth(480);
    else return;
    event.preventDefault();
  }

  function statusClass(status: FileChangeStatus): string {
    if (status === 'added') return 'text-lime-300';
    if (status === 'deleted') return 'text-rose-300';
    if (status === 'renamed') return 'text-violet-300';
    return 'text-amber-300';
  }

  function statusLabel(status: FileChangeStatus): string {
    return { added: 'A', modified: 'M', deleted: 'D', renamed: 'R' }[status];
  }

  function isBinaryDiff(diff: WorkFileDiff): boolean {
    return diff.binary === true || /^Binary files /m.test(diff.diff);
  }

  function unavailablePreviewCount(file: FileTreeFile): number {
    return file.diffs.filter(
      (event) => event.preview_omitted
        || event.generated
        || isBinaryDiff(event)
        || !event.diff.trim()
    ).length;
  }

  function omittedPreviewCount(file: FileTreeFile): number {
    return file.diffs.filter((event) => event.preview_omitted).length;
  }

  function sourceLabels(file: FileTreeFile): string[] {
    return [...new Set(file.diffs.flatMap((event) => event.source_workstream
      ? [`${event.source_workstream.agent_id} · ${event.source_workstream.title} · ${event.source_workstream.status}`]
      : []
    ))];
  }

  onDestroy(() => {
    explorerObserver?.disconnect();
    if (cacheKey) setWorkFileTreeState(cacheKey, {
      query,
      statusFilter,
      expanded: [...expanded],
      selectedId,
      treeScrollTop: treeElement?.scrollTop ?? 0,
      diffScrollTop: diffElement?.scrollTop ?? 0,
    });
  });
</script>

{#snippet stats(additions: number, deletions: number)}
  <span class="ml-auto shrink-0 font-mono text-[10px] tabular-nums">
    {#if additions}<span class="text-lime-300">+{additions}</span>{/if}
    {#if additions && deletions}<span class="text-slate-600">/</span>{/if}
    {#if deletions}<span class="text-rose-300">-{deletions}</span>{/if}
  </span>
{/snippet}

{#snippet diffHistory(file: FileTreeFile)}
  <div class="space-y-3" data-testid="work-combined-diff">
    {#if sourceLabels(file).length}
      <p class="text-[10px] text-slate-500">{sourceLabels(file).join(' · ')}</p>
    {/if}
    {#if combineFileDiffHistory(file)}
      {#if combineFileDiffHistory(file)?.truncated}
        <p class="rounded-lg border border-yellow-500/30 bg-yellow-500/10 px-3 py-2 text-xs text-yellow-100">
          The combined diff contains content-truncated previews. The available partial history appears below.
        </p>
      {/if}
      {#if unavailablePreviewCount(file)}
        <p class="rounded-lg border border-slate-700 bg-slate-900/60 px-3 py-2 text-xs text-slate-400" data-testid="work-diff-partial-history">
          {unavailablePreviewCount(file)} change{unavailablePreviewCount(file) === 1 ? '' : 's'} without a text preview {unavailablePreviewCount(file) === 1 ? 'is' : 'are'} not included in the combined diff.
        </p>
      {/if}
      <FileDiffViewer
        diffs={[combineFileDiffHistory(file)!]}
        collapsible={false}
        onExpand={() => { focusedDiffOpen = true; }}
      />
    {:else if omittedPreviewCount(file)}
      <div class="rounded-xl border border-dashed border-amber-500/30 p-6 text-center" data-testid="work-diff-preview-omitted">
        <p class="text-sm text-amber-100">This file has exact change totals, but its diff preview was omitted by the projection file limit.</p>
        <p class="mt-1 text-xs text-slate-500">{file.path}</p>
      </div>
    {:else}
      <div class="rounded-xl border border-dashed border-slate-700 p-6 text-center" data-testid="work-diff-no-preview">
        <p class="text-sm text-slate-300">No text preview is available.</p>
        <p class="mt-1 text-xs text-slate-500">{file.binary ? 'Binary file' : file.generated ? 'Generated file' : 'Text preview unavailable'} · {file.path}</p>
      </div>
    {/if}
  </div>
{/snippet}

<div bind:this={explorerElement} class="flex min-h-[28rem] min-w-0 overflow-hidden rounded-xl border border-slate-700/80 bg-slate-950/40" data-testid="work-file-explorer">
  {#if treeCollapsed}
    <button
      type="button"
      class="work-tree-wide-only hidden w-11 shrink-0 items-start justify-center border-r border-slate-800 pt-3 text-slate-400 hover:text-slate-100"
      aria-label="Show files tree"
      title="Show files tree"
      onclick={() => { treeCollapsed = false; }}
    ><PanelLeftOpen class="h-4 w-4" /></button>
  {:else}
    <aside
      class={`${mobileDiffOpen ? 'hidden work-tree-pane-open' : 'flex'} work-tree-pane relative w-full shrink-0 flex-col border-r border-slate-800`}
      style:width={explorerWidth >= 880 && !mobileDiffOpen ? `${treeWidth}px` : undefined}
      data-testid="work-files-tree-pane"
    >
      <div class="sticky top-0 z-10 space-y-2 border-b border-slate-800 bg-slate-950/95 p-2">
        <div class="flex items-center gap-2">
          <label class="relative min-w-0 flex-1">
            <span class="sr-only">Filter changed files</span>
            <Search class="pointer-events-none absolute left-2.5 top-2.5 h-3.5 w-3.5 text-slate-500" />
            <input bind:this={filterInput} bind:value={query} class="h-9 w-full rounded-lg border border-slate-700 bg-slate-900 pl-8 pr-2 text-xs text-slate-100 placeholder:text-slate-600" placeholder="Filter files…" />
          </label>
          <button type="button" class="work-tree-wide-only hidden rounded p-2 text-slate-500 hover:bg-slate-800 hover:text-slate-200" aria-label="Hide files tree" title="Hide files tree" onclick={() => { treeCollapsed = true; }}><PanelLeftClose class="h-4 w-4" /></button>
        </div>
        <div class="flex items-center gap-2">
          <select bind:value={statusFilter} class="h-8 min-w-0 flex-1 rounded-lg border border-slate-800 bg-slate-900 px-2 text-xs text-slate-300" aria-label="Filter by file status">
            <option value="all">All statuses</option>
            <option value="added">Added</option>
            <option value="modified">Modified</option>
            <option value="deleted">Deleted</option>
            <option value="renamed">Renamed</option>
          </select>
          <span class="text-[10px] text-slate-500">{files.length} files</span>
          {@render stats(totals.additions, totals.deletions)}
        </div>
      </div>
       <div bind:this={treeElement} role="tree" tabindex="-1" aria-label="Changed files" class="min-h-0 flex-1 overflow-y-auto p-1" onkeydown={onTreeKeydown} data-testid="work-files-tree">
        {#if commonRoot}
          <p class="px-2 py-1 font-mono text-[10px] text-slate-500" data-testid="work-files-common-root">{commonRoot}</p>
        {/if}
        {#each visible as item (item.node.id)}
          <button
            type="button"
            role="treeitem"
            aria-level={item.node.depth}
            aria-expanded={item.node.kind === 'folder' ? effectiveExpanded.has(item.node.id) : undefined}
            aria-selected={item.node.kind === 'file' ? selected?.id === item.node.id : false}
            tabindex={focusedId === item.node.id ? 0 : -1}
            data-node-id={item.node.id}
            data-testid={item.node.kind === 'file' ? `work-file-${item.node.diff.path_id ?? item.node.path}` : undefined}
            class={`flex min-h-9 w-full min-w-0 items-center gap-1.5 rounded px-1.5 text-left text-xs ${item.node.kind === 'file' && selected?.id === item.node.id ? 'bg-sky-500/15 text-sky-100' : 'text-slate-300 hover:bg-slate-800/80'}`}
            style:padding-left={`${Math.max(6, item.node.depth * 14)}px`}
            onclick={() => item.node.kind === 'folder' ? toggleFolder(item.node) : selectFile(item.node)}
            onfocus={() => { focusedId = item.node.id; }}
          >
            {#if item.node.kind === 'folder'}
              {#if effectiveExpanded.has(item.node.id)}<ChevronDown class="h-3.5 w-3.5 shrink-0" /><FolderOpen class="h-3.5 w-3.5 shrink-0 text-sky-300" />{:else}<ChevronRight class="h-3.5 w-3.5 shrink-0" /><Folder class="h-3.5 w-3.5 shrink-0 text-sky-300" />{/if}
              <span class="scrollbar-hidden-x min-w-0">{item.node.name}</span>
              <span class="rounded bg-slate-800 px-1.5 text-[10px] text-slate-500">{item.node.counts.files}</span>
              {@render stats(item.node.counts.additions, item.node.counts.deletions)}
            {:else}
              <span class={`w-3 shrink-0 font-mono text-[10px] font-bold ${statusClass(item.node.status)}`}>{statusLabel(item.node.status)}</span>
              <FileCode2 class="h-3.5 w-3.5 shrink-0 text-slate-500" />
              <span class="scrollbar-hidden-x min-w-0" title={item.node.path}>{item.node.name}</span>
              {#if item.node.binary}<span class="text-[9px] text-violet-300">binary</span>{:else if item.node.generated}<span class="text-[9px] text-slate-500">generated</span>{:else if item.node.truncated}<span class="text-[9px] text-yellow-300">truncated</span>{/if}
              {@render stats(item.node.counts.additions, item.node.counts.deletions)}
            {/if}
          </button>
        {/each}
        {#if visible.length === 0}<p class="p-4 text-center text-xs text-slate-500" data-testid="work-files-empty-filter">No changed files match this filter.</p>{/if}
      </div>
      <!-- svelte-ignore a11y_no_noninteractive_tabindex, a11y_no_noninteractive_element_interactions -->
      <div
        role="separator"
        tabindex="0"
        aria-label="Resize files tree"
        aria-orientation="vertical"
        aria-valuemin="200"
        aria-valuemax="480"
        aria-valuenow={treeWidth}
        class="work-tree-wide-only absolute -right-1 top-0 z-20 hidden h-full w-2 cursor-col-resize touch-none focus-visible:bg-sky-400/40"
        onpointerdown={startResize}
        onkeydown={resizeWithKeyboard}
        data-testid="work-files-resizer"
      ></div>
    </aside>
  {/if}

  <main bind:this={diffElement} class={`${mobileDiffOpen ? 'block' : 'hidden work-diff-wide'} min-w-0 max-w-full flex-1 overflow-auto p-2 sm:p-3`} data-testid="work-diff-pane">
    {#if selected}
      <button type="button" class="work-tree-narrow-only mb-2 inline-flex min-h-10 items-center gap-2 rounded-lg px-2 text-sm text-sky-200 hover:bg-slate-800" onclick={() => { mobileDiffOpen = false; }}><ArrowLeft class="h-4 w-4" />Back to files</button>
      <div class="mb-2 flex min-w-0 items-center gap-2 text-xs">
        <span class={`font-mono font-bold ${statusClass(selected.status)}`}>{statusLabel(selected.status)}</span>
        <span class="scrollbar-hidden-x min-w-0 font-mono text-slate-300">{selected.diff.old_path ? `${selected.diff.old_path} → ${selected.path}` : selected.path}</span>
        {@render stats(selected.counts.additions, selected.counts.deletions)}
      </div>
      {@render diffHistory(selected)}
    {:else}
      <p class="p-8 text-center text-sm text-slate-500">Select a changed file.</p>
    {/if}
  </main>
</div>

{#if focusedDiffOpen && selected && combineFileDiffHistory(selected)}
  <div class="fixed inset-0 z-50 flex flex-col bg-slate-950/95 backdrop-blur" role="dialog" aria-modal="true" aria-label={`Expanded diff for ${selected.path}`} data-testid="work-diff-overlay">
    <header class="flex items-center gap-3 border-b border-slate-800 px-4 py-3">
      <span class="scrollbar-hidden-x min-w-0 flex-1 font-mono text-xs text-slate-200">{selected.path}</span>
      <button type="button" class="inline-flex items-center gap-2 rounded-lg border border-slate-700 px-3 py-2 text-xs text-slate-200" onclick={() => { focusedDiffOpen = false; }} aria-label="Minimize diff">
        <Minimize2 class="h-4 w-4" /> Minimize
      </button>
    </header>
    <div class="min-h-0 flex-1 overflow-auto p-4">
      <FileDiffViewer diffs={[combineFileDiffHistory(selected)!]} collapsible={false} />
    </div>
  </div>
{/if}

<style>
  @container work (min-width: 55rem) {
    .work-tree-wide-only,
    .work-tree-pane-open { display: flex; }
    .work-diff-wide { display: block; }
    .work-tree-pane { width: auto; }
    .work-tree-narrow-only { display: none; }
  }
</style>
