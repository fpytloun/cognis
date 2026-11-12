<script lang="ts">
  import { api } from '$lib/api/client';
  import BlockingDialog from '$lib/components/ui/BlockingDialog.svelte';
  import ResourceReader from './ResourceReader.svelte';
  import type { LibraryItem, LibraryKind } from '$lib/library';

  let { kind, ref, onClose, onChanged }: { kind: LibraryKind; ref: string; onClose: () => void; onChanged?: (item: LibraryItem) => void } = $props();
  let item = $state<LibraryItem | null>(null);
  let error = $state('');
  let retry = $state(0);
  let dirty = $state(false);
  let busy = $state(false);
  function close() { if (!busy && (!dirty || confirm('Discard unsaved artifact changes?'))) onClose(); }
  $effect(() => {
    const id = ref;
    const type = kind;
    void retry;
    let cancelled = false;
    item = null; error = '';
    void api.library.item(type, id).then((value) => {
      if (!cancelled) item = value;
    }).catch((caught) => {
      if (!cancelled) error = caught instanceof Error ? caught.message : 'Resource unavailable.';
    });
    return () => { cancelled = true; };
  });
</script>

<BlockingDialog label={item?.title || 'Resource'} onClose={close} backdropDismissible={!dirty} dismissible={!busy} panelClass="w-[min(96vw,1480px)] max-w-none h-[90dvh] max-sm:h-full max-sm:w-full max-sm:rounded-2xl" bodyClass="!p-0 !overflow-hidden"
  viewportClass="max-sm:px-[max(.375rem,env(safe-area-inset-left))] max-sm:pr-[max(.375rem,env(safe-area-inset-right))] [--resource-modal-gap:1rem] max-sm:[--resource-modal-gap:.375rem]"
  viewportStyle="padding-top: calc(var(--app-shell-top-offset, 0px) + env(safe-area-inset-top) + var(--resource-modal-gap)); padding-bottom: calc(var(--app-shell-bottom-offset, 0px) + var(--app-safe-area-bottom) + var(--resource-modal-gap));">
  <div class="h-full min-h-0" data-testid="library-resource-modal">
    {#if item}
      {#key item.id}<ResourceReader {item} onClose={close} onDirtyChange={(value) => { dirty = value; }} onBusyChange={(value) => { busy = value; }} onRevision={(updated) => { item = updated; onChanged?.(updated); }} />{/key}
    {:else if error}
      <div role="alert"><p class="text-danger">{error}</p><button class="min-h-11 text-accent" onclick={() => retry++}>Retry</button></div>
    {:else}<p role="status" class="text-sm text-muted">Loading resource…</p>{/if}
  </div>
</BlockingDialog>
