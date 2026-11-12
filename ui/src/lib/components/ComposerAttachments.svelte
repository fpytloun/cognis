<script lang="ts">
  import Mic from 'lucide-svelte/icons/mic';
  import Pause from 'lucide-svelte/icons/pause';
  import Play from 'lucide-svelte/icons/play';
  import X from 'lucide-svelte/icons/x';
  import { api } from '$lib/api/client';
  import { previewKind } from '$lib/attachments/preview';
  import type { SignedHtmlPreview } from '$lib/attachments/html-preview';
  import { attachmentUploads, isLocalAttachment } from '$lib/chat-v2/attachment-uploads';
  import { onDestroy } from 'svelte';
  import { documentCoordination, type DocumentLease } from '$lib/document-coordination';
  import FeedbackHost from './feedback/FeedbackHost.svelte';
  import { getFeedbackContext } from '$lib/feedback/context';

  import { haptic } from '$lib/haptics';
  import type { AttachmentRef } from '$lib/types/api';
  import { formatVoiceDuration } from '$lib/utils/voice-recorder';

  /**
   * Composer attachment preview strip.
   *
   * Renders image attachments as small thumbnails (familiar messaging-app
   * affordance), audio recordings as inline mini-players, and everything
   * else as a filename chip. Every item has an overlay remove button so
   * the user can drop an attachment before sending. Reused by the inline
   * and expanded composer to keep previews consistent.
   */

  interface Props {
    attachments: AttachmentRef[];
    onremove: (artifactId: string) => void;
    /** Retry a failed background upload (local attachments only). */
    onretry?: (attachment: AttachmentRef) => void;
    disabled?: boolean;
    class?: string;
  }

  let { attachments, onremove, onretry, disabled = false, class: className = '' }: Props = $props();

  // Attachments can be annotated before sending. Comments made here are staged in this chat and
  // go out with the message; removing the attachment first deletes them. `commented` maps the
  // composer id of every attachment opened for commenting to its uploaded artifact id.
  const origin = getFeedbackContext();
  const commented = new Map<string, string>();
  let commentRef = $state<string | null>(null);
  function remove(artifactId: string) {
    onremove(artifactId);
    const uploaded = commented.get(artifactId);
    commented.delete(artifactId);
    if (uploaded) void discardComments(uploaded);
  }
  async function discardComments(contentRef: string) {
    try {
      const { feedbackApi } = await import('$lib/feedback/api');
      const source = await feedbackApi.resolve({ kind: 'document', content_ref: contentRef }, origin.context);
      const page = await feedbackApi.threads(source.subject_id);
      const own = page.items.filter(thread => !thread.hidden && thread.entries[0]?.author.is_self);
      await Promise.all(own.map(thread => feedbackApi.changeThread(thread, { hidden: true })));
      if (own.length) window.dispatchEvent(new Event('feedback-changed'));
    } catch {
      // Best effort: an unreachable source has nothing left to include.
    }
  }

  function uploadPercent(attachment: AttachmentRef): number {
    return Math.round(Math.max(0, Math.min(1, attachment.upload_progress ?? 0)) * 100);
  }

  function uploadLabel(attachment: AttachmentRef): string {
    if (attachment.upload_state === 'failed') return 'Upload failed';
    return `Uploading ${uploadPercent(attachment)}%`;
  }

  let playingId = $state<string | null>(null);
  let selected = $state<AttachmentRef | null>(null);
  let previewMediaUrl = $state<string | null>(null);
  let previewHtmlSource = $state<SignedHtmlPreview | null>(null);
  let previewGeneration = 0;
  let previewAttachmentId = $state<string | null>(null);
  let previewController: AbortController | null = null;
  let previewError = $state('');
  onDestroy(() => { previewGeneration += 1; previewController?.abort(); });
  function closePreview() {
    previewGeneration += 1;
    previewController?.abort();
    selected = null;
    previewAttachmentId = null;
  }
  $effect(() => {
    if (previewAttachmentId && !attachments.some(attachment => attachment.artifact_id === previewAttachmentId)) closePreview();
  });
  function canPreview(attachment: AttachmentRef): boolean {
    return isImage(attachment) || ((!isLocalAttachment(attachment) || attachment.upload_state === 'uploaded') && Boolean(previewKind(attachment)));
  }
  async function openPreview(attachment: AttachmentRef) {
    const generation = ++previewGeneration;
    previewController?.abort();
    const controller = new AbortController();
    previewController = controller;
    previewAttachmentId = attachment.artifact_id;
    previewError = '';
    if (isLocalAttachment(attachment) && (!isImage(attachment) || !(attachment.blob_url || attachment.url))) {
      try {
        attachment = await attachmentUploads.resolve(attachment);
      } catch {
        if (generation === previewGeneration) previewError = 'Could not open attachment. Please retry.';
        return;
      }
    }
    if (generation !== previewGeneration) return;
    let htmlSource: SignedHtmlPreview | null = null;
    let mediaUrl = attachment.blob_url ?? attachment.url ?? null;
    if (previewKind(attachment) === 'html') {
      let lease: DocumentLease | null = null;
      try {
        lease = documentCoordination().capture();
        const { resolveHtmlPreviewUrl } = await import('$lib/attachments/html-preview-private');
        await lease.validate();
        lease.signal.throwIfAborted();
        controller.signal.throwIfAborted();
        htmlSource = await resolveHtmlPreviewUrl(attachment.artifact_id, controller.signal);
      } catch {
        if (lease?.signal.aborted || controller.signal.aborted || generation !== previewGeneration) return;
        previewError = 'Could not open attachment. Please retry.';
        return;
      }
    } else if (!mediaUrl && (isImage(attachment) || ['pdf', 'video', 'audio'].includes(previewKind(attachment) ?? ''))) {
      mediaUrl = await api.artifacts.signedUrl(attachment.artifact_id, 3600, 'download').then(result => result.url).catch(() => null);
    }
    if (generation !== previewGeneration || controller.signal.aborted) return;
    if (isImage(attachment) && !mediaUrl) {
      previewError = 'Could not open attachment. Please retry.';
      return;
    }
    // Comments need the uploaded artifact; a file still uploading is viewable but not commentable.
    let uploadedId: string | null = isLocalAttachment(attachment) ? null : attachment.artifact_id;
    if (!uploadedId && attachment.upload_state === 'uploaded') {
      uploadedId = await attachmentUploads.resolve(attachment).then(ref => ref.artifact_id).catch(() => null);
      if (generation !== previewGeneration || controller.signal.aborted) return;
    }
    if (uploadedId) commented.set(previewAttachmentId ?? attachment.artifact_id, uploadedId);
    commentRef = uploadedId;
    previewMediaUrl = mediaUrl;
    previewHtmlSource = htmlSource;
    selected = attachment;
  }
  const audioElements = new Map<string, HTMLAudioElement>();

  function isImage(attachment: AttachmentRef): boolean {
    return typeof attachment.mime_type === 'string' && attachment.mime_type.startsWith('image/');
  }

  function isAudio(attachment: AttachmentRef): boolean {
    if (attachment.voice_recording) return true;
    return typeof attachment.mime_type === 'string' && attachment.mime_type.startsWith('audio/');
  }

  function audioUrl(attachment: AttachmentRef): string | null {
    return attachment.blob_url ?? attachment.url ?? null;
  }

  function togglePlay(attachment: AttachmentRef): void {
    const id = attachment.artifact_id;
    let audio = audioElements.get(id);
    if (!audio) {
      const url = audioUrl(attachment);
      if (!url) return;
      audio = new Audio(url);
      audio.onended = () => {
        if (playingId === id) playingId = null;
      };
      audioElements.set(id, audio);
    }
    if (playingId === id) {
      audio.pause();
      playingId = null;
      return;
    }
    audioElements.forEach((other, key) => {
      if (key !== id) other.pause();
    });
    void audio.play().then(() => {
      playingId = id;
      haptic.light();
    }).catch(() => {
      playingId = null;
    });
  }
</script>

{#if attachments.length > 0}
  <div class={`flex flex-wrap gap-2 ${className}`}>
    {#each attachments as attachment (attachment.artifact_id)}
      {#if isImage(attachment) && attachment.url}
        <div class="group relative h-20 w-20 overflow-hidden rounded-xl border border-control-border bg-surface">
          <button type="button" class="h-full w-full" aria-label={`Preview ${attachment.filename}`} onclick={() => void openPreview(attachment)}>
          <img
            alt={attachment.filename}
            class="h-full w-full object-cover"
            loading="lazy"
            src={attachment.url}
          />
          </button>
          {#if attachment.upload_state === 'uploading'}
            <div
              class="pointer-events-none absolute inset-x-0 bottom-0 bg-scrim/70 px-1 py-0.5 text-center text-[10px] font-medium tabular-nums text-white"
              role="status"
              aria-label={uploadLabel(attachment)}
            >
              {uploadPercent(attachment)}%
              <div class="mt-0.5 h-0.5 w-full overflow-hidden rounded bg-white/25">
                <div class="h-full bg-white transition-[width]" style={`width: ${uploadPercent(attachment)}%`}></div>
              </div>
            </div>
          {:else if attachment.upload_state === 'failed'}
            <button
              type="button"
              class="absolute inset-x-0 bottom-0 bg-danger-subtle py-0.5 text-[10px] font-medium text-danger"
              title={attachment.upload_error ?? 'Upload failed'}
              onclick={() => onretry?.(attachment)}
            >
              Retry
            </button>
          {/if}
          <button
            aria-label={`Remove ${attachment.filename}`}
            class="absolute right-1 top-1 inline-flex h-6 w-6 items-center justify-center rounded-full bg-scrim/85 text-white shadow-md transition hover:bg-danger-subtle hover:text-danger disabled:cursor-not-allowed disabled:opacity-50"
            {disabled}
            onclick={() => remove(attachment.artifact_id)}
            type="button"
          >
            <X class="h-3.5 w-3.5" />
          </button>
        </div>
      {:else if isAudio(attachment)}
        <div class="flex items-center gap-2 rounded-full border border-control-border bg-inset/70 px-3 py-1.5 text-xs text-foreground">
          <button
            aria-label={playingId === attachment.artifact_id ? 'Pause recording' : 'Play recording'}
            class="inline-flex h-7 w-7 items-center justify-center rounded-full bg-surface text-foreground transition hover:bg-surface-hover disabled:opacity-50"
            type="button"
            onclick={() => togglePlay(attachment)}
            disabled={!audioUrl(attachment)}
          >
            {#if playingId === attachment.artifact_id}
              <Pause class="h-3.5 w-3.5" />
            {:else}
              <Play class="h-3.5 w-3.5" />
            {/if}
          </button>
          <Mic class="h-3.5 w-3.5 text-pink-tone" />
          <span class="font-medium tabular-nums">{formatVoiceDuration(attachment.duration_seconds ?? 0)}</span>
          {#if attachment.voice_recording}
            <span class="text-[10px] uppercase tracking-wider text-muted">ready</span>
          {/if}
          <button
            aria-label={`Remove ${attachment.filename}`}
            class="text-muted transition hover:text-strong disabled:cursor-not-allowed disabled:opacity-50"
            {disabled}
            onclick={() => { haptic.warning(); remove(attachment.artifact_id); }}
            type="button"
          >
            <X class="h-3.5 w-3.5" />
          </button>
        </div>
      {:else}
        <div class="flex items-center gap-2 rounded-full border border-control-border bg-inset/70 px-3 py-2 text-xs text-foreground">
          <button type="button" class="max-w-[220px] truncate text-left hover:underline disabled:no-underline" disabled={!canPreview(attachment)} aria-label={`Preview ${attachment.filename}`} onclick={() => void openPreview(attachment)}>{attachment.filename}</button>
          {#if attachment.upload_state === 'uploading'}
            <span class="tabular-nums text-info" role="status" aria-label={uploadLabel(attachment)}>{uploadPercent(attachment)}%</span>
          {:else if attachment.upload_state === 'failed'}
            <button
              type="button"
              class="font-medium text-danger hover:underline"
              title={attachment.upload_error ?? 'Upload failed'}
              onclick={() => onretry?.(attachment)}
            >
              Retry
            </button>
          {/if}
          <button
            aria-label={`Remove ${attachment.filename}`}
            class="text-muted transition hover:text-strong disabled:cursor-not-allowed disabled:opacity-50"
            {disabled}
            onclick={() => remove(attachment.artifact_id)}
            type="button"
          >
            <X class="h-3.5 w-3.5" />
          </button>
        </div>
      {/if}
    {/each}
  </div>
{/if}
{#if previewError}<p class="text-xs text-danger" role="alert">{previewError}</p>{/if}

{#if selected}
  {#if isImage(selected) && previewMediaUrl}
    {#await import('./ImageLightbox.svelte') then { default: ImageLightbox }}
    <FeedbackHost source={commentRef ? { kind: 'document', content_ref: commentRef } : null}>
      <ImageLightbox src={previewMediaUrl} alt={selected.filename} filename={selected.filename} onClose={closePreview} />
    </FeedbackHost>
    {/await}
  {:else}
    {@const kind = previewKind(selected)}
    {#if kind}
      {#await import('./AttachmentPreviewModal.svelte') then { default: AttachmentPreviewModal }}
        <AttachmentPreviewModal attachment={selected} {kind} mediaUrl={previewMediaUrl} htmlSource={previewHtmlSource} onClose={closePreview}
          resolveDownloadUrl={() => selected ? api.artifacts.signedUrl(selected.artifact_id, 3600, 'download').then(result => result.url) : Promise.resolve(null)} />
      {/await}
    {/if}
  {/if}
{/if}
