<script lang="ts">
  import ChevronDown from 'lucide-svelte/icons/chevron-down';
  import ChevronRight from 'lucide-svelte/icons/chevron-right';
  import FileText from 'lucide-svelte/icons/file-text';
  import Folder from 'lucide-svelte/icons/folder';
  import FolderOpen from 'lucide-svelte/icons/folder-open';

  import { cn } from '$lib/utils';
  import type { DocumentTreeNode } from '$lib/knowledge/tree';
  import DocumentTree from './DocumentTree.svelte';

  let {
    nodes,
    selectedPath = null,
    expanded,
    onSelect,
    onToggle,
    depth = 0
  }: {
    nodes: DocumentTreeNode[];
    selectedPath?: string | null;
    expanded: Set<string>;
    onSelect: (node: DocumentTreeNode) => void;
    onToggle: (path: string) => void;
    depth?: number;
  } = $props();

  function handleKeydown(event: KeyboardEvent, node: DocumentTreeNode): void {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      if (node.kind === 'folder') {
        onToggle(node.path);
      } else {
        onSelect(node);
      }
    } else if (node.kind === 'folder' && event.key === 'ArrowRight' && !expanded.has(node.path)) {
      event.preventDefault();
      onToggle(node.path);
    } else if (node.kind === 'folder' && event.key === 'ArrowLeft' && expanded.has(node.path)) {
      event.preventDefault();
      onToggle(node.path);
    } else if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
      event.preventDefault();
      const tree = (event.currentTarget as HTMLElement).closest('[role="tree"]');
      const items = Array.from(tree?.querySelectorAll<HTMLElement>('[role="treeitem"]') ?? []);
      const current = items.indexOf(event.currentTarget as HTMLElement);
      const target =
        event.key === 'Home' ? items[0]
        : event.key === 'End' ? items.at(-1)
        : event.key === 'ArrowDown' ? items[current + 1]
        : items[current - 1];
      target?.focus();
    }
  }
</script>

<ul role={depth === 0 ? 'tree' : 'group'} class="flex flex-col" aria-label={depth === 0 ? 'Document browser' : undefined}>
  {#each nodes as node (node.kind === 'file' ? `file:${node.path}:${node.document.doc_id}` : `folder:${node.path}`)}
    <li role="none">
      {#if node.kind === 'folder'}
        <div role="none" style={`padding-left: ${depth * 14}px`}>
          <button
            type="button"
            role="treeitem"
            aria-expanded={expanded.has(node.path)}
            aria-selected={selectedPath === node.path}
            tabindex="0"
            class="flex w-full items-center gap-1.5 rounded-lg px-2 py-1.5 text-left text-sm text-slate-300 hover:bg-slate-800/70"
            onclick={() => onToggle(node.path)}
            onkeydown={(event) => handleKeydown(event, node)}
            data-testid="knowledge-tree-folder"
          >
            {#if expanded.has(node.path)}
              <ChevronDown class="h-3.5 w-3.5 shrink-0 text-slate-500" />
              <FolderOpen class="h-4 w-4 shrink-0 text-amber-300" />
            {:else}
              <ChevronRight class="h-3.5 w-3.5 shrink-0 text-slate-500" />
              <Folder class="h-4 w-4 shrink-0 text-amber-300" />
            {/if}
            <span class="truncate">{node.name}</span>
          </button>
        </div>
        {#if expanded.has(node.path)}
          <DocumentTree nodes={node.children} {selectedPath} {expanded} {onSelect} {onToggle} depth={depth + 1} />
        {/if}
      {:else}
        <div role="none" style={`padding-left: ${depth * 14 + 20}px`}>
          <button
            type="button"
            role="treeitem"
            aria-selected={selectedPath === node.path}
            tabindex="0"
            class={cn(
              'flex w-full items-center gap-1.5 rounded-lg px-2 py-1.5 text-left text-sm hover:bg-slate-800/70',
              selectedPath === node.path ? 'bg-sky-500/15 text-sky-200' : 'text-slate-300'
            )}
            onclick={() => onSelect(node)}
            onkeydown={(event) => handleKeydown(event, node)}
            data-testid="knowledge-tree-file"
          >
            <FileText class="h-4 w-4 shrink-0 text-slate-500" />
            <span class="truncate">{node.name}</span>
          </button>
        </div>
      {/if}
    </li>
  {/each}
</ul>
