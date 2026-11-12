<script lang="ts">
  import { api } from '$lib/api/client';
  import { hasRevisionHistory, isSupersededRevision, revisionLabel } from '$lib/deliverable-revisions';
  import type { Deliverable, DeliverableRevisionSummary } from '$lib/types/api';
  import type { RevisionOption } from './RevisionSwitcher.svelte';
  // Most deliverables have one revision; load the switcher only for those with history.
  const switcher = () => import('./RevisionSwitcher.svelte');

  // `current` is the revision on screen; `anchor` is the revision this surface
  // was opened for. History is loaded lazily, only when the user opens it.
  // Notices stay with the document while the switcher lives in its shared action toolbar.
  let {
    current,
    anchorId = current.deliverable_id,
    onSelect,
    part = 'all',
    menu = false,
  }: {
    current: Deliverable;
    anchorId?: string;
    onSelect: (revision: DeliverableRevisionSummary) => void;
    part?: 'all' | 'notice' | 'switcher';
    menu?: boolean;
  } = $props();

  let summaries = new Map<string, DeliverableRevisionSummary>();
  const visible = $derived(hasRevisionHistory(current));
  const superseded = $derived(isSupersededRevision(current));
  const latestId = $derived(current.latest_deliverable_id ?? null);
  const showNotice = $derived(part !== 'switcher' && (superseded || current.deliverable_id !== anchorId));

  async function loadRevisions(): Promise<RevisionOption[]> {
    const result = await api.deliverables.revisions(anchorId);
    summaries = new Map(result.revisions.map(revision => [revision.deliverable_id, revision]));
    return result.revisions.map(revision => ({
      id: revision.deliverable_id, revision: revision.revision, title: revision.title,
      author_email: revision.author_email, author_agent_id: revision.author_agent_id,
      note: revision.task_id ? 'task output' : null, created_at: revision.created_at, is_latest: revision.is_latest,
    }));
  }
  function select(option: RevisionOption) {
    const summary = summaries.get(option.id);
    if (summary && summary.deliverable_id !== current.deliverable_id) onSelect(summary);
  }
  // The summary carries the owning step run, which selects the detail endpoint.
  async function showLatest(): Promise<void> {
    if (!summaries.size) await loadRevisions();
    const latest = (latestId ? summaries.get(latestId) : undefined) ?? [...summaries.values()].find(item => item.is_latest);
    if (latest && latest.deliverable_id !== current.deliverable_id) onSelect(latest);
  }
</script>

{#if visible && (part !== 'notice' || showNotice)}
  {#if menu}
    {#await switcher() then { default: RevisionSwitcher }}
    <RevisionSwitcher menu label={revisionLabel(current.revision, current.latest_revision)} currentId={current.deliverable_id}
      load={loadRevisions} onSelect={select} testid="deliverable-revision" />
    {/await}
  {:else}
  <div class="revision-bar" class:revision-controls={part === 'switcher'} data-testid="deliverable-revision-bar" data-superseded={superseded ? 'true' : undefined}>
    <div class="flex flex-wrap items-center gap-2 text-xs">
      {#if part !== 'notice'}
        {#await switcher() then { default: RevisionSwitcher }}
        <RevisionSwitcher label={revisionLabel(current.revision, current.latest_revision)} currentId={current.deliverable_id}
          load={loadRevisions} onSelect={select} testid="deliverable-revision" />
        {/await}
      {/if}
      {#if showNotice && superseded}
        <span class="rounded-full border border-warning-border bg-warning-subtle px-2.5 py-1 text-warning" data-testid="deliverable-superseded-notice">
          Superseded by revision {current.latest_revision}
        </span>
        {#if latestId}
          <button type="button" class="text-accent hover:text-accent-hover" data-testid="deliverable-view-latest" onclick={() => void showLatest()}>View latest</button>
        {/if}
      {:else if showNotice}
        <span class="rounded-full border border-info-border bg-info-subtle px-2.5 py-1 text-info">Latest</span>
      {/if}
    </div>
  </div>
  {/if}
{/if}

<style>
  .revision-bar {
    margin-bottom: 0.5rem;
  }
  .revision-controls { margin-bottom: 0; }
</style>
