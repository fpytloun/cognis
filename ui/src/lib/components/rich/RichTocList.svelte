<script lang="ts">
  import type { TocItem, TocNode } from './publication';

  export let nodes: TocNode[] = [];
  export let activeAnchor = '';
  export let onNavigate: (item: TocItem) => void;
  export let depth = 0;
</script>

<ol class:document-toc={depth === 0} class:rich-document-toc={depth === 0}>
  {#each nodes as node}
    <li data-level={node.item.level}>
      <button
        type="button"
        style={`--toc-depth: ${depth}`}
        class:active={activeAnchor === node.item.anchor}
        aria-current={activeAnchor === node.item.anchor ? 'location' : undefined}
        on:click={() => onNavigate(node.item)}
      >
        {#if node.item.level === 2 && /^\d+\s/.test(node.item.label)}
          <span class="toc-number">{node.item.label.match(/^\d+/)?.[0]}</span><span>{node.item.label.replace(/^\d+\s+/, '')}</span>
        {:else}<span>{node.item.label}</span>{/if}
      </button>
      {#if node.children.length > 0}
        <svelte:self nodes={node.children} {activeAnchor} {onNavigate} depth={depth + 1} />
      {/if}
    </li>
  {/each}
</ol>
