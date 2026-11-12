<script lang="ts" module>
  export interface RevisionOption {
    id: string;
    revision: number;
    title?: string | null;
    note?: string | null;
    created_at: string | null;
    is_latest: boolean;
    author_email?: string | null;
    author_agent_id?: string | null;
  }
</script>
<script lang="ts">
  import { anchoredMenu } from '$lib/actions/anchored-menu';
  import History from 'lucide-svelte/icons/history';

  /**
   * The one revision switcher for artifacts and deliverables. Inline it is a chip that opens the
   * revision list in the top layer; inside a ⋯ menu (`menu`) it is a row that expands the list in
   * place. History loads lazily on first open.
   */
  let { label, currentId, load, onSelect, menu = false, testid = 'revision' }: {
    label: string;
    currentId: string;
    load: () => Promise<RevisionOption[]>;
    onSelect: (revision: RevisionOption) => void;
    menu?: boolean;
    testid?: string;
  } = $props();

  let open = $state(false);
  let loading = $state(false);
  let error = $state('');
  let revisions = $state<RevisionOption[] | null>(null);
  let details = $state<HTMLDetailsElement>();

  async function ensureLoaded() {
    if (revisions || loading) return;
    loading = true; error = '';
    try { revisions = await load(); }
    catch (caught) { error = caught instanceof Error ? caught.message : 'Could not load revisions'; }
    finally { loading = false; }
  }
  function toggleRow() { open = !open; if (open) void ensureLoaded(); }
  function choose(revision: RevisionOption) {
    open = false;
    if (details) details.open = false;
    if (revision.id !== currentId) onSelect(revision);
  }
  function formatDate(value: string | null): string {
    if (!value) return '';
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? '' : date.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
  }
  function dismiss(event: PointerEvent) {
    if (details?.open && event.target instanceof Node && !details.contains(event.target)) details.open = false;
  }
  function escape(event: KeyboardEvent) {
    if (event.key !== 'Escape' || !details?.open) return;
    event.stopPropagation();
    details.open = false;
    details.querySelector('summary')?.focus();
  }
</script>

<svelte:window onpointerdown={dismiss} />
{#snippet list()}
  <div class="revision-list" role="listbox" aria-label="Revisions" data-testid={`${testid}-list`}>
    {#if loading}<p class="revision-status">Loading revisions…</p>
    {:else if error}<p class="revision-status text-danger">{error}</p>
    {:else if revisions}
      {#each revisions as revision (revision.id)}
        <button type="button" role="option" aria-selected={revision.id === currentId} class="revision-option" class:current={revision.id === currentId}
          data-testid={`${testid}-option`} data-revision-id={revision.id}
          data-deliverable-id={testid === 'deliverable-revision' ? revision.id : undefined} data-menu-close onkeydown={escape} onclick={() => choose(revision)}>
          <span class="min-w-0">
            <span class="font-medium">Revision {revision.revision}</span>
            {#if revision.title}<span class="ml-1 truncate text-muted">· {revision.title}</span>{/if}
            {#if revision.note}<span class="ml-1 text-quiet">· {revision.note}</span>{/if}
            <span class="block text-quiet">By {revision.author_agent_id ?? revision.author_email ?? 'Unknown author'}</span>
          </span>
          <span class="flex shrink-0 items-center gap-2 text-quiet">
            {formatDate(revision.created_at)}
            {#if revision.is_latest}<span class="rounded-full bg-info-subtle px-1.5 py-0.5 text-info">Latest</span>{/if}
          </span>
        </button>
      {/each}
    {/if}
  </div>
{/snippet}

{#if menu}
  <button type="button" aria-expanded={open} data-testid={`${testid}-toggle`} onclick={toggleRow}>
    <History aria-hidden="true" /><span>{label}</span>
  </button>
  {#if open}{@render list()}{/if}
{:else}
  <details class="revision-switcher" bind:this={details} ontoggle={() => { if (details?.open) void ensureLoaded(); }}>
    <summary class="revision-chip" aria-haspopup="listbox" data-testid={`${testid}-toggle`}
      onkeydown={escape} onclick={() => void ensureLoaded()}>{label}<span aria-hidden="true" class="text-quiet">▾</span></summary>
    <div class="revision-panel" use:anchoredMenu={{ preferredSide: 'below' }}>{@render list()}</div>
  </details>
{/if}

<style>
  .revision-switcher { position: relative; flex: none; }
  summary { list-style: none; }
  summary::-webkit-details-marker { display: none; }
  .revision-chip {
    display: inline-flex; align-items: center; gap: .25rem; min-height: var(--rich-action-size, 2rem); padding: 0 .65rem;
    border: 1px solid var(--rich-line, rgb(var(--color-control-border))); border-radius: 999px; background: var(--rich-control-bg, rgb(var(--color-surface) / .8));
    color: var(--rich-text, rgb(var(--color-foreground))); font-size: 12px; font-weight: 500; cursor: pointer; white-space: nowrap;
  }
  @media (hover: hover) { .revision-chip:hover { border-color: rgb(var(--color-accent)); } }
  .revision-panel {
    position: absolute; right: 0; top: calc(100% + .4rem); z-index: 40; width: max-content; min-width: 16rem; max-width: calc(100vw - 2rem);
    padding: .25rem; border: 1px solid var(--rich-line, rgb(var(--color-control-border))); border-radius: .75rem;
    background: var(--rich-surface-raised, rgb(var(--color-elevated))); color: var(--rich-text, rgb(var(--color-foreground))); box-shadow: 0 8px 24px #0003;
  }
  .revision-list { display: flex; flex-direction: column; }
  .revision-status { padding: .5rem .75rem; font-size: 12px; color: rgb(var(--color-muted)); }
  .revision-option {
    display: flex; width: 100%; align-items: center; justify-content: space-between; gap: .75rem; min-height: 2.25rem;
    padding: .4rem .75rem; border: 0; border-radius: .5rem; background: transparent; color: inherit;
    font-size: 12px; text-align: left;
  }
  .revision-option:hover, .revision-option.current { background: var(--rich-control-active, rgb(var(--color-surface-hover))); }
  .revision-chip:focus-visible, .revision-option:focus-visible { outline: 2px solid var(--rich-accent, rgb(var(--color-accent))); outline-offset: 2px; }
  @media (pointer: coarse) { .revision-chip, .revision-option { min-height: 44px; } }
</style>
