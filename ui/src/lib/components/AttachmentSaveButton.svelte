<script lang="ts">
  import { onDestroy } from 'svelte';
  import Download from 'lucide-svelte/icons/download';
  import Share from 'lucide-svelte/icons/share';
  import { canShareFile, downloadFile, isAppleMobile, prepareAttachmentFile } from '$lib/attachments/save';
  import { keepInViewport } from '$lib/actions/keep-in-viewport';

  let { filename, resolveUrl, class: className = 'copy-icon-button', label = '' }: {
    filename: string;
    resolveUrl: () => Promise<string | null>;
    class?: string;
    /** Visible text beside the icon, used when the control sits in a menu. */
    label?: string;
  } = $props();

  let busy = $state(false);
  let ready = $state<File | null>(null);
  let error = $state<string | null>(null);
  let controller: AbortController | null = null;
  let disposed = false;
  onDestroy(() => { disposed = true; controller?.abort(); ready = null; });

  async function save(): Promise<void> {
    if (busy) return;
    error = null;
    if (ready) {
      // A second, explicit tap preserves activation even after a slow fetch.
      busy = true;
      try {
        await navigator.share({ files: [ready], title: filename });
        ready = null;
      } catch (cause) {
        if (!(cause instanceof Error && cause.name === 'AbortError')) {
          error = 'Saving failed. Tap Save/share again to retry.';
        }
      } finally { busy = false; }
      return;
    }
    busy = true;
    controller = new AbortController();
    try {
      const url = await resolveUrl();
      if (disposed) return;
      if (!url) throw new Error('The download URL could not be loaded. Please try again.');
      const file = await prepareAttachmentFile(url, filename, controller.signal);
      if (disposed) return;
      if (isAppleMobile()) {
        if (!canShareFile(file)) {
          throw new Error('This browser cannot save this file in-app. Open Cognis in Safari to save it.');
        }
        ready = file;
      } else {
        downloadFile(file);
      }
    } catch (cause) {
      if (!disposed) error = cause instanceof Error ? cause.message : 'The file could not be saved.';
    } finally { busy = false; }
  }
</script>

<span class="relative inline-flex shrink-0">
  <button
    type="button"
    class={className}
    disabled={busy}
    aria-label={ready ? `Save/share ${filename}` : `Download ${filename}`}
    title={ready ? 'Save/share file — choose Save to Files' : 'Download'}
    onclick={() => { void save(); }}
  >
    {#if busy}<span class="text-xs">…</span>
    {:else if ready}<Share class="h-5 w-5" />
    {:else}<Download class="h-5 w-5" />{/if}
    {#if label}<span>{ready ? 'Save/share' : label}</span>{/if}
  </button>
  {#if ready || error}
    <span role="status" class="absolute right-0 top-full z-20 mt-1 w-56 rounded-lg border border-control-border bg-elevated p-2 text-xs text-foreground shadow-lg" use:keepInViewport>
      {error ?? 'File ready. Tap Save/share, then choose Save to Files.'}
    </span>
  {/if}
</span>
