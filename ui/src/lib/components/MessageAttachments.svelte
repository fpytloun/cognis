<script lang="ts">
  import Eye from 'lucide-svelte/icons/eye';
  import DocumentCommentCount from './DocumentCommentCount.svelte';
  import FeedbackHost from '$lib/components/feedback/FeedbackHost.svelte';
  import FileText from 'lucide-svelte/icons/file-text';

  import { api } from '$lib/api/client';
  import type { SignedHtmlPreview } from '$lib/attachments/html-preview';
  import { documentCoordination, type DocumentLease } from '$lib/document-coordination';
  import { onDestroy } from 'svelte';
  import { previewKind } from '$lib/attachments/preview';
  import { attachmentUploads } from '$lib/chat-v2/attachment-uploads';
  import DeferredPrivateView from './ui/DeferredPrivateView.svelte';
  import { imageLightbox } from './ui/private-view-definitions';
  import AttachmentSaveButton from './AttachmentSaveButton.svelte';
  import type { AttachmentRef } from '$lib/types/api';

  /**
   * Render attachments inside a chat message bubble in an iMessage-style:
   *
   * - Image attachments collapse to a row of square thumbnails (96px on
   *   mobile, 112px on sm+). Tapping a thumbnail opens a full-screen
   *   lightbox with a download button.
    * - Non-image attachments offer in-app preview and a shared file-save control.
   *
   * The component contains its own lightbox state so it can be dropped
   * anywhere a chat message renders attachments without callers needing
   * to wire popover state.
   */

  let { attachments: rawAttachments } = $props<{ attachments: AttachmentRef[] }>();

  // A sent-but-not-yet-admitted message holds the attachment references as
  // they were at send time. Overlay the live background-upload state so the
  // bubble keeps showing progress (and failures) until the upload finishes.
  type UploadPatch = Pick<AttachmentRef, 'upload_state' | 'upload_progress' | 'upload_error'>;
  let livePatches = $state<Record<string, UploadPatch>>({});

  $effect(() => {
    const tracked = new Set<string>();
    for (const attachment of rawAttachments as AttachmentRef[]) {
      if (attachment.local_id) tracked.add(attachment.local_id);
    }
    if (tracked.size === 0) return;
    const initial: Record<string, UploadPatch> = {};
    for (const id of tracked) {
      const state = attachmentUploads.uploadState(id);
      if (state) initial[id] = state;
    }
    livePatches = initial;
    return attachmentUploads.onChange((localId, patch) => {
      if (!tracked.has(localId)) return;
      livePatches = { ...livePatches, [localId]: { ...livePatches[localId], ...patch } };
    });
  });

  const attachments = $derived(
    rawAttachments.map((attachment: AttachmentRef) => (
      attachment.local_id && livePatches[attachment.local_id]
        ? { ...attachment, ...livePatches[attachment.local_id] }
        : attachment
    )),
  );

  function uploadPercent(attachment: AttachmentRef): number {
    return Math.round(Math.max(0, Math.min(1, attachment.upload_progress ?? 0)) * 100);
  }

  function isUploading(attachment: AttachmentRef): boolean {
    return Boolean(attachment.local_id) && attachment.upload_state === 'uploading';
  }

  function uploadFailed(attachment: AttachmentRef): boolean {
    return Boolean(attachment.local_id) && attachment.upload_state === 'failed';
  }

  let lightboxIndex = $state<number | null>(null);
  let previewAttachment = $state<AttachmentRef | null>(null);
  let previewMediaUrl = $state<string | null>(null);
  let previewHtmlSource = $state<SignedHtmlPreview | null>(null);
  let previewRequest = 0;
  let previewController: AbortController | null = null;
  onDestroy(() => { previewRequest += 1; previewController?.abort(); });
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
      artifact_id: image.local_id ? undefined : image.artifact_id,
    })),
  );

  async function resolveAttachmentUrl(attachment: AttachmentRef, mode: 'download' | 'view' = 'download'): Promise<string | null> {
    const existing = resolvedUrl(attachment, mode);
    if (existing) return existing;
    // Still uploading in the background: there is no server artifact yet.
    if (attachment.local_id || attachment.artifact_id.startsWith('local:')) return null;
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
      if (
        typeof attachment.mime_type === 'string'
        && (attachment.mime_type.startsWith('image/') || attachment.mime_type.startsWith('audio/'))
      ) {
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

  async function openPreview(attachment: AttachmentRef): Promise<void> {
    const request = ++previewRequest;
    previewController?.abort();
    const controller = new AbortController();
    previewController = controller;
    const kind = previewKind(attachment);
    previewAttachment = null;
    let htmlSource: SignedHtmlPreview | null = null;
    if (kind === 'html') {
      let lease: DocumentLease | null = null;
      try {
        lease = documentCoordination().capture();
        const { resolveHtmlPreviewUrl } = await import('$lib/attachments/html-preview-private');
        if (request !== previewRequest || controller.signal.aborted) return;
        await lease.validate();
        lease.signal.throwIfAborted();
        controller.signal.throwIfAborted();
        htmlSource = await resolveHtmlPreviewUrl(attachment.artifact_id, controller.signal);
      } catch {
        if (lease?.signal.aborted || controller.signal.aborted) return;
      }
    }
    const mediaUrl = ['pdf', 'video', 'audio'].includes(kind ?? '')
        ? await resolveAttachmentUrl(attachment, 'download')
        : null;
    if (request !== previewRequest || controller.signal.aborted) return;
    previewHtmlSource = htmlSource;
    previewMediaUrl = mediaUrl;
    previewAttachment = attachment;
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
      <div class="flex shrink-0 flex-col items-center gap-1">
      <button
        type="button"
        class="group relative h-24 w-24 shrink-0 overflow-hidden rounded-2xl border border-separator bg-inset transition hover:border-border-strong focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent sm:h-28 sm:w-28"
        onclick={() => openLightbox(index)}
        aria-label={`View ${image.filename}`}
      >
        <img
          src={resolvedUrl(image, 'download') ?? ''}
          alt={image.filename}
          class="h-full w-full object-cover transition group-hover:scale-105"
          loading="lazy"
        />
        {#if isUploading(image)}
          <span
            class="pointer-events-none absolute inset-x-0 bottom-0 bg-scrim/70 px-1 py-0.5 text-center text-[10px] font-medium tabular-nums text-white"
            role="status"
            aria-label={`Uploading ${uploadPercent(image)}%`}
          >
            {uploadPercent(image)}%
            <span class="mt-0.5 block h-0.5 w-full overflow-hidden rounded bg-white/25">
              <span class="block h-full bg-white transition-[width]" style={`width: ${uploadPercent(image)}%`}></span>
            </span>
          </span>
        {:else if uploadFailed(image)}
          <span
            class="pointer-events-none absolute inset-x-0 bottom-0 bg-danger-subtle py-0.5 text-center text-[10px] font-medium text-danger"
            title={image.upload_error ?? 'Upload failed'}
          >
            Upload paused
          </span>
        {/if}
      </button>
      <DocumentCommentCount id={image.artifact_id} />
      </div>
    {/each}
  </div>
{/if}

{#if otherAttachments.length > 0}
  <div class="mt-3 space-y-1.5">
    {#each otherAttachments as attachment (attachment.artifact_id)}
      {@const sizeText = formatBytes(attachment.size_bytes)}
      {@const kind = previewKind(attachment)}
      <div class="flex items-center gap-3 rounded-xl border border-separator bg-surface px-3 py-2 text-foreground">
        <span class="inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-raised text-secondary">
          <FileText class="h-4 w-4" />
        </span>
        <div class="min-w-0 flex-1">
          <!-- Long names scroll sideways without a visible scrollbar instead of overflowing the card.
               A button shrinks to its text rather than stretching, so it sits inside the scroller. -->
          <div class="scrollbar-hidden-x">
            {#if kind}
              <button
                type="button"
                onclick={() => { void openPreview(attachment); }}
                class="text-left text-sm font-medium text-accent hover:text-accent-hover"
              >
                {attachment.filename}
              </button>
            {:else}
              <p class="text-sm font-medium text-foreground">{attachment.filename}</p>
            {/if}
          </div>
          <p class="truncate text-xs text-secondary">
            {attachment.mime_type ?? 'file'}{sizeText ? ` · ${sizeText}` : ''}
            {#if isUploading(attachment)}
              <span class="tabular-nums text-info" role="status"> · Uploading {uploadPercent(attachment)}%</span>
            {:else if uploadFailed(attachment)}
              <span class="text-danger" title={attachment.upload_error ?? 'Upload failed'}> · Upload paused</span>
            {/if}
          </p>
          <DocumentCommentCount id={attachment.artifact_id} />
          {#if isUploading(attachment)}
            <span class="mt-1 block h-0.5 w-full overflow-hidden rounded bg-raised">
              <span class="block h-full bg-info transition-[width]" style={`width: ${uploadPercent(attachment)}%`}></span>
            </span>
          {/if}
        </div>
        {#if attachment.mime_type?.startsWith('audio/')}
          {@const audioUrl = resolvedUrl(attachment, 'download')}
          {#if audioUrl}
            <audio class="h-9 max-w-48" src={audioUrl} controls preload="none"></audio>
          {:else}
            <button type="button" onclick={() => { void resolveAttachmentUrl(attachment, 'download'); }} class="text-xs text-accent hover:text-accent-hover">Load player</button>
          {/if}
        {/if}
        {#if kind}
          <button
            type="button"
            onclick={() => { void openPreview(attachment); }}
            aria-label={`Preview ${attachment.filename}`}
            class="inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-lg text-muted transition hover:bg-surface-hover hover:text-foreground"
          >
            <Eye class="h-4 w-4" />
          </button>
        {/if}
        <AttachmentSaveButton
          filename={attachment.filename}
          resolveUrl={() => resolveAttachmentUrl(attachment, 'download')}
          class="inline-flex min-h-11 min-w-11 items-center justify-center rounded-lg text-muted hover:bg-surface-hover hover:text-foreground"
        />
      </div>
    {/each}
  </div>
{/if}

{#if previewAttachment}
  {@const activePreviewKind = previewKind(previewAttachment)}
  {#if activePreviewKind}
    {#await import('$lib/components/AttachmentPreviewModal.svelte') then { default: AttachmentPreviewModal }}
    <AttachmentPreviewModal
      attachment={previewAttachment}
      kind={activePreviewKind}
      mediaUrl={previewMediaUrl}
      htmlSource={previewHtmlSource}
      onClose={() => { previewRequest += 1; previewController?.abort(); previewAttachment = null; previewMediaUrl = null; previewHtmlSource = null; }}
      resolveDownloadUrl={() => previewAttachment ? resolveAttachmentUrl(previewAttachment, 'download') : Promise.resolve(null)}
    />
    {/await}
  {/if}
{/if}

{#if lightboxIndex !== null && imageAttachments[lightboxIndex]}
  {@const current = imageAttachments[lightboxIndex]}
  <FeedbackHost source={current.artifact_id ? { kind: 'document', content_ref: current.artifact_id } : null}>
  <DeferredPrivateView definition={imageLightbox} label="image preview" modal onClose={closeLightbox} viewProps={{
    src: resolvedUrl(current, 'download') ?? '', alt: current.filename, filename: current.filename,
    images: lightboxImages, index: lightboxIndex,
    onIndexChange: (nextIndex) => { lightboxIndex = nextIndex; }, onClose: closeLightbox,
  }} />
  </FeedbackHost>
{/if}
