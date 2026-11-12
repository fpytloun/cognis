<script lang="ts">
  import { api } from '$lib/api/client';
  import ResourceReader from '$lib/components/library/ResourceReader.svelte';
  import FeedbackContinuity from '$lib/components/feedback/FeedbackContinuity.svelte';
  import { libraryHref, type LibraryItem } from '$lib/library';
  import type { WorkspaceManager } from '$lib/dashboard/workspace';

  let { kind, id, manager, windowKey }: {
    kind: 'artifact' | 'deliverable';
    id: string;
    manager: WorkspaceManager;
    windowKey: string;
  } = $props();

  let item = $state<LibraryItem | null>(null);
  let error = $state('');
  let retry = $state(0);
  let dirty = $state(false);
  let busy = $state(false);
  const resourceKind = $derived(kind);
  const resourceId = $derived(id);

  $effect(() => manager.registerCloseGuard(windowKey, () => (
    !busy && (!dirty || confirm('Discard unsaved artifact changes?'))
  )));

  $effect(() => {
    const type = resourceKind;
    const ref = resourceId;
    void retry;
    let cancelled = false;
    item = null;
    error = '';
    void api.library.item(type, ref).then((value) => {
      if (!cancelled) {
        item = value;
        manager.updateMetadata(windowKey, { title: value.title || 'Untitled document' });
      }
    }).catch((caught) => {
      if (!cancelled) error = caught instanceof Error ? caught.message : 'Resource unavailable.';
    });
    return () => { cancelled = true; };
  });
</script>

<div class="flex h-full min-h-0 flex-col" data-testid="dashboard-document">
  {#if item}
    <FeedbackContinuity contentRef={item.id} />
    <ResourceReader {item}
      onRevision={(updated) => manager.navigateDocument(windowKey, {
        kind: updated.kind,
        entityId: updated.id,
        title: updated.title || 'Untitled document',
        canonicalHref: libraryHref(updated),
      })}
      onDirtyChange={(value) => { dirty = value; }}
      onBusyChange={(value) => { busy = value; }}
    />
  {:else if error}
    <div role="alert" class="p-6 text-sm">
      <p class="text-danger">{error}</p>
      <button class="min-h-11 text-accent" onclick={() => retry++}>Retry</button>
    </div>
  {:else}
    <p role="status" class="p-6 text-sm text-muted">Loading resource…</p>
  {/if}
</div>
