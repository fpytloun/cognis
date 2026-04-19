<script lang="ts">
  import Download from 'lucide-svelte/icons/download';
  import FileText from 'lucide-svelte/icons/file-text';

  import ImageLightbox from '$lib/components/ImageLightbox.svelte';
  import type { AttachmentRef } from '$lib/types/api';

  /**
   * Render attachments inside a chat message bubble in an iMessage-style:
   *
   * - Image attachments collapse to a row of square thumbnails (96px on
   *   mobile, 112px on sm+). Tapping a thumbnail opens a full-screen
   *   lightbox with a download button.
   * - Non-image attachments render as compact one-line file pills with a
   *   filename, size hint, and an explicit download icon. The whole pill
   *   is also a link, so a regular tap opens the file in a new tab.
   *
   * The component contains its own lightbox state so it can be dropped
   * anywhere a chat message renders attachments without callers needing
   * to wire popover state.
   */

  let { attachments } = $props<{ attachments: AttachmentRef[] }>();

  let lightboxIndex = $state<number | null>(null);

  const imageAttachments = $derived(
    attachments.filter(
      (a: AttachmentRef): a is AttachmentRef & { url: string } =>
        Boolean(a.url) && typeof a.mime_type === 'string' && a.mime_type.startsWith('image/'),
    ),
  );

  const otherAttachments = $derived(
    attachments.filter(
      (a: AttachmentRef) =>
        !(typeof a.mime_type === 'string' && a.mime_type.startsWith('image/') && Boolean(a.url)),
    ),
  );

  function openLightbox(index: number): void {
    lightboxIndex = index;
  }

  function closeLightbox(): void {
    lightboxIndex = null;
  }

  function formatBytes(value: number | null | undefined): string {
    if (typeof value !== 'number' || value <= 0) return '';
    if (value < 1024) return `${value} B`;
    if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
    if (value < 1024 * 1024 * 1024) return `${(value / 1024 / 1024).toFixed(1)} MB`;
    return `${(value / 1024 / 1024 / 1024).toFixed(2)} GB`;
  }
</script>

{#if imageAttachments.length > 0}
  <div class="mt-3 flex flex-wrap gap-2">
    {#each imageAttachments as image, index (image.artifact_id)}
      <button
        type="button"
        class="group relative h-24 w-24 shrink-0 overflow-hidden rounded-2xl border border-slate-800/60 bg-slate-950/60 transition hover:border-slate-600 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-400 sm:h-28 sm:w-28"
        onclick={() => openLightbox(index)}
        aria-label={`View ${image.filename}`}
      >
        <img
          src={image.url}
          alt={image.filename}
          class="h-full w-full object-cover transition group-hover:scale-105"
          loading="lazy"
        />
      </button>
    {/each}
  </div>
{/if}

{#if otherAttachments.length > 0}
  <div class="mt-3 space-y-1.5">
    {#each otherAttachments as attachment (attachment.artifact_id)}
      {@const sizeText = formatBytes(attachment.size_bytes)}
      <div class="flex items-center gap-3 rounded-xl border border-slate-800/60 bg-slate-950/40 px-3 py-2">
        <span class="inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-slate-800/60 text-slate-300">
          <FileText class="h-4 w-4" />
        </span>
        <div class="min-w-0 flex-1">
          {#if attachment.url}
            <a
              href={attachment.url}
              target="_blank"
              rel="noreferrer"
              class="block truncate text-sm font-medium text-slate-100 hover:text-sky-300"
            >
              {attachment.filename}
            </a>
          {:else}
            <p class="truncate text-sm font-medium text-slate-100">{attachment.filename}</p>
          {/if}
          <p class="truncate text-xs text-slate-500">
            {attachment.mime_type ?? 'file'}{sizeText ? ` · ${sizeText}` : ''}
          </p>
        </div>
        {#if attachment.url}
          <a
            href={attachment.url}
            download={attachment.filename}
            target="_blank"
            rel="noreferrer"
            aria-label={`Download ${attachment.filename}`}
            class="inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-lg text-slate-400 transition hover:bg-slate-800/60 hover:text-slate-100"
          >
            <Download class="h-4 w-4" />
          </a>
        {/if}
      </div>
    {/each}
  </div>
{/if}

{#if lightboxIndex !== null && imageAttachments[lightboxIndex]}
  {@const current = imageAttachments[lightboxIndex]}
  <ImageLightbox
    src={current.url}
    alt={current.filename}
    filename={current.filename}
    onClose={closeLightbox}
  />
{/if}
