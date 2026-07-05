<script lang="ts">
  import Download from 'lucide-svelte/icons/download';
  import FileText from 'lucide-svelte/icons/file-text';

  import { api } from '$lib/api/client';
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
  let resolvedUrls = $state<Record<string, string>>({});

  function urlKey(attachment: AttachmentRef, mode: 'download' | 'view'): string {
    return `${attachment.artifact_id}:${mode}`;
  }

  function resolvedUrl(attachment: AttachmentRef, mode: 'download' | 'view' = 'view'): string | null {
    if (mode === 'download') return attachment.url ?? resolvedUrls[urlKey(attachment, mode)] ?? null;
    return resolvedUrls[urlKey(attachment, mode)] ?? null;
  }

  const imageAttachments = $derived(
    attachments.filter(
      (a: AttachmentRef) =>
        Boolean(resolvedUrl(a, 'download')) && typeof a.mime_type === 'string' && a.mime_type.startsWith('image/'),
    ),
  );

  const otherAttachments = $derived(
    attachments.filter(
      (a: AttachmentRef) =>
        !(typeof a.mime_type === 'string' && a.mime_type.startsWith('image/') && Boolean(resolvedUrl(a, 'download'))),
    ),
  );

  const lightboxImages = $derived(
    imageAttachments.map((image: AttachmentRef) => ({
      src: resolvedUrl(image, 'download') ?? '',
      alt: image.filename,
      filename: image.filename,
    })),
  );

  async function resolveAttachmentUrl(attachment: AttachmentRef, mode: 'download' | 'view' = 'download'): Promise<string | null> {
    const existing = resolvedUrl(attachment, mode);
    if (existing) return existing;
    try {
      const result = await api.artifacts.signedUrl(attachment.artifact_id, 3600, mode);
      resolvedUrls = { ...resolvedUrls, [urlKey(attachment, mode)]: result.url };
      return result.url;
    } catch (error) {
      console.error('Failed to resolve artifact URL', error);
      return null;
    }
  }

  $effect(() => {
    for (const attachment of attachments) {
      if (attachment.url || resolvedUrls[urlKey(attachment, 'download')]) continue;
      if (typeof attachment.mime_type === 'string' && attachment.mime_type.startsWith('image/')) {
        void resolveAttachmentUrl(attachment, 'download');
      }
    }
  });

  function openLightbox(index: number): void {
    lightboxIndex = index;
  }

  function closeLightbox(): void {
    lightboxIndex = null;
  }

  function isHtmlAttachment(attachment: AttachmentRef): boolean {
    return attachment.mime_type?.split(';', 1)[0]?.trim().toLowerCase() === 'text/html';
  }

  async function openViewAttachment(event: MouseEvent, attachment: AttachmentRef): Promise<void> {
    event.preventDefault();
    const popup = window.open('', '_blank');
    if (popup) {
      popup.opener = null;
    }
    try {
      const url = await resolveAttachmentUrl(attachment, 'view');
      if (!url) throw new Error('Unable to resolve artifact URL');
      if (popup) {
        popup.location.href = url;
      } else {
        window.open(url, '_blank', 'noopener,noreferrer');
      }
    } catch (error) {
      console.error('Failed to open artifact view URL', error);
      if (popup) {
        popup.close();
      }
      if (attachment.url) {
        window.open(attachment.url, '_blank', 'noopener,noreferrer');
      }
    }
  }

  async function openDownloadAttachment(attachment: AttachmentRef): Promise<void> {
    const url = await resolveAttachmentUrl(attachment, 'download');
    if (url) window.open(url, '_blank', 'noopener,noreferrer');
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
        class="group relative h-24 w-24 shrink-0 overflow-hidden rounded-2xl border border-slate-800/60 bg-slate-950/60 transition hover:border-slate-600 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-300 sm:h-28 sm:w-28"
        onclick={() => openLightbox(index)}
        aria-label={`View ${image.filename}`}
      >
        <img
          src={resolvedUrl(image, 'download') ?? ''}
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
          {#if resolvedUrl(attachment, 'download') || isHtmlAttachment(attachment)}
            <a
              href={resolvedUrl(attachment, 'download') ?? '#'}
              target="_blank"
              rel="noopener noreferrer"
              onclick={isHtmlAttachment(attachment) ? (event) => { void openViewAttachment(event, attachment); } : undefined}
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
        {#if resolvedUrl(attachment, 'download')}
          <a
            href={resolvedUrl(attachment, 'download') ?? ''}
            download={attachment.filename}
            target="_blank"
            rel="noopener noreferrer"
            aria-label={`Download ${attachment.filename}`}
            class="inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-lg text-slate-400 transition hover:bg-slate-800/60 hover:text-slate-100"
          >
            <Download class="h-4 w-4" />
          </a>
        {:else}
          <button
            type="button"
            onclick={() => { void openDownloadAttachment(attachment); }}
            aria-label={`Download ${attachment.filename}`}
            class="inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-lg text-slate-400 transition hover:bg-slate-800/60 hover:text-slate-100"
          >
            <Download class="h-4 w-4" />
          </button>
        {/if}
      </div>
    {/each}
  </div>
{/if}

{#if lightboxIndex !== null && imageAttachments[lightboxIndex]}
  {@const current = imageAttachments[lightboxIndex]}
  <ImageLightbox
    src={resolvedUrl(current, 'download') ?? ''}
    alt={current.filename}
    filename={current.filename}
    images={lightboxImages}
    index={lightboxIndex}
    onIndexChange={(nextIndex) => { lightboxIndex = nextIndex; }}
    onClose={closeLightbox}
  />
{/if}
