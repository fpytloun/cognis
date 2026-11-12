<script lang="ts">
  import X from 'lucide-svelte/icons/x';
  import Search from 'lucide-svelte/icons/search';
  import Button from './ui/Button.svelte';
  import { requestFind } from '$lib/find';
  import FeedbackHost from '$lib/components/feedback/FeedbackHost.svelte';
  import FeedbackIndicator from '$lib/components/feedback/FeedbackIndicator.svelte';
  import FeedbackWorkspace from '$lib/components/feedback/FeedbackWorkspace.svelte';
  import { getContext, onMount, tick, untrack } from 'svelte';
  import RevisionSwitcher, { type RevisionOption } from './RevisionSwitcher.svelte';
  import { previewKind } from '$lib/attachments/preview';
  import type { ArtifactRevisionSummary } from '$lib/types/api';
  import { readable, type Readable } from 'svelte/store';
  import { DELIVERABLE_SHARING_CONTEXT } from '$lib/deliverable-permissions';

  import { portal } from '$lib/actions/portal';
  import { api } from '$lib/api/client';
  import type { AttachmentPreviewKind } from '$lib/attachments/preview';
  import ArtifactPreviewContent from './ArtifactPreviewContent.svelte';
  import AttachmentSaveButton from './AttachmentSaveButton.svelte';
  import CompactHeaderActions from './CompactHeaderActions.svelte';
  import { isTopOverlay, registerOverlay } from '$lib/stores/overlays';
  import type { AttachmentRef } from '$lib/types/api';
  import DocumentLinkActions from './rich/DocumentLinkActions.svelte';
  import DocumentFavoriteButton from './DocumentFavoriteButton.svelte';
  import DeferredPrivateView from './ui/DeferredPrivateView.svelte';
  import { deliverableAudio } from './ui/private-view-definitions';
  import { appDocumentAudioClient } from '$lib/app-document-audio';
  import type { ShareLinkOptions } from './rich/share-options';
  import { viewEligible } from '$lib/artifact-text';
  import type { SignedHtmlPreview } from '$lib/attachments/html-preview';

  let {
    attachment,
    kind,
    mediaUrl = null,
    htmlSource = null,
    onClose,
    resolveDownloadUrl,
  }: {
    attachment: AttachmentRef;
    kind: AttachmentPreviewKind;
    mediaUrl?: string | null;
    htmlSource?: SignedHtmlPreview | null;
    onClose: () => void;
    resolveDownloadUrl: () => Promise<string | null>;
  } = $props();

  // The revision on screen. It starts as the opened attachment and follows the revision switcher.
  let shown = $state(untrack(() => ({ attachment, kind, mediaUrl, htmlSource })));
  let revisions = $state<ArtifactRevisionSummary[]>([]);
  let switching: AbortController | null = null;
  const opened = untrack(() => attachment.artifact_id);
  async function showRevision(option: RevisionOption) {
    const summary = revisions.find(revision => revision.artifact_id === option.id);
    if (!summary) return;
    switching?.abort();
    const controller = switching = new AbortController();
    const next: AttachmentRef = { ...shown.attachment, artifact_id: summary.artifact_id, filename: summary.filename, mime_type: summary.mime_type, size_bytes: summary.size_bytes };
    const nextKind = previewKind(next) ?? 'text';
    loading = true; error = null; content = ''; richPayload = null; truncated = false;
    try {
      let nextMedia: string | null = null, nextHtml: SignedHtmlPreview | null = null;
      let nextContent = '', nextTruncated = false, nextRichPayload: unknown = null;
      if (nextKind === 'html') {
        const { resolveHtmlPreviewUrl } = await import('$lib/attachments/html-preview-private');
        nextHtml = await resolveHtmlPreviewUrl(next.artifact_id, controller.signal);
      } else if (['pdf', 'video', 'audio'].includes(nextKind)) {
        nextMedia = (await api.artifacts.signedUrl(next.artifact_id, 3600, nextKind === 'pdf' ? 'view' : 'download')).url;
      } else {
        const result = await api.artifacts.textPreview(next.artifact_id);
        nextContent = result.content; nextTruncated = result.truncated; nextRichPayload = result.rich_payload;
      }
      if (controller.signal.aborted) return;
      content = nextContent; truncated = nextTruncated; richPayload = nextRichPayload;
      shown = { attachment: next, kind: nextKind, mediaUrl: nextMedia, htmlSource: nextHtml };
    } catch {
      if (!controller.signal.aborted) error = 'That revision could not be loaded.';
    } finally {
      if (!controller.signal.aborted) loading = false;
    }
  }
  const downloadUrl = () => shown.attachment.artifact_id === opened ? resolveDownloadUrl()
    : api.artifacts.signedUrl(shown.attachment.artifact_id, 3600, 'download').then(result => result.url);

  let panel = $state<HTMLDivElement | null>(null);
  function searchDocument(event?: MouseEvent) {
    const details = event?.currentTarget instanceof Element ? event.currentTarget.closest('details') : null;
    if (details) { details.open = false; details.querySelector('summary')?.focus(); }
    const scope = panel?.querySelector<HTMLElement>('[data-find-scope]');
    if (scope) requestFind(scope);
  }
  let closeButton = $state<HTMLButtonElement | null>(null);
  let overlayId = $state<string | null>(null);
  let content = $state('');
  let truncated = $state(false);
  let richPayload = $state<unknown>(null);
  let loading = $state(true);
  let error = $state<string | null>(null);
  const searchable = $derived(!loading && !error && ['text', 'markdown', 'mermaid', 'csv', 'html', 'pdf'].includes(shown.kind));
  const sharingPermission = getContext<Readable<boolean>>(DELIVERABLE_SHARING_CONTEXT) ?? readable(true);
  async function createPublicLink(options: ShareLinkOptions): Promise<string> {
    return (await api.artifactDocuments.publicLink('artifact', shown.attachment.artifact_id, { ...options, mode: options.mode ?? 'download' })).url;
  }

  function handleKeydown(event: KeyboardEvent): void {
    if (!isTopOverlay(overlayId)) return;
    if (event.key === 'Escape' && event.target instanceof Element && event.target.closest('[data-find-bar]')) return;
    if (event.key === 'Escape') {
      event.preventDefault();
      onClose();
      return;
    }
    if (event.key !== 'Tab' || !panel) return;
    const elements = Array.from(panel.querySelectorAll<HTMLElement>('button:not([disabled]), input:not([disabled]), [href], [controls], [tabindex]:not([tabindex="-1"])'))
      .filter(node => node.getClientRects().length > 0 && getComputedStyle(node).visibility !== 'hidden');
    if (elements.length === 0) return;
    const first = elements[0];
    const last = elements[elements.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  onMount(() => {
    const initial = switching = new AbortController();
    let disposed = false;
    const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const overlay = registerOverlay({ kind: 'fullscreen', blocksChrome: true, root: () => panel });
    overlayId = overlay.id;
    void tick().then(() => closeButton?.focus());
    // Revision history is optional: the preview never depends on it loading.
    void Promise.resolve().then(() => api.artifacts.revisions(attachment.artifact_id))
      .then(history => { if (!disposed) revisions = history.revisions; }).catch(() => {});
    if (['text', 'markdown', 'mermaid', 'csv'].includes(kind)) {
      void api.artifacts.textPreview(attachment.artifact_id).then((result) => {
        if (initial.signal.aborted) return;
        content = result.content;
        truncated = result.truncated;
        richPayload = result.rich_payload;
      }).catch(() => {
        if (!initial.signal.aborted) error = 'The attachment preview could not be loaded.';
      }).finally(() => {
        if (!initial.signal.aborted) loading = false;
      });
    } else {
      loading = false;
    }
    return () => {
      disposed = true;
      switching?.abort();
      overlay.unregister();
      overlayId = null;
      if (previousFocus?.isConnected) previousFocus.focus();
    };
  });
</script>

<svelte:window onkeydown={handleKeydown} />

<FeedbackHost source={{ kind: 'document', content_ref: shown.attachment.artifact_id }}>
<div data-find-container use:portal bind:this={panel} class="attachment-preview app-viewport-overlay app-safe-fullscreen z-[90] isolate overflow-hidden bg-surface text-foreground" role="dialog" aria-modal="true" aria-labelledby="attachment-preview-title" tabindex="-1" data-blocking-overlay>
    <header data-find-toolbar class="attachment-preview-header app-safe-fullscreen__toolbar flex min-w-0 items-center justify-between gap-2 border-b border-separator bg-chrome py-1">
      <div class="flex min-w-0 flex-1 items-center gap-1">
        <div class="min-w-0">
          <h2 id="attachment-preview-title" class="min-w-0 truncate text-lg font-semibold text-foreground" title={shown.attachment.filename}>{shown.attachment.filename}</h2>
          <p class="mt-0.5 truncate text-xs text-muted">{shown.attachment.mime_type ?? 'file'}</p>
        </div>
        <DocumentFavoriteButton kind="artifact" id={shown.attachment.artifact_id} />
      </div>
      <div data-find-actions class="flex shrink-0 items-center gap-2">
        {#if searchable}<Button variant="ghost" size="icon" class="document-icon-action" aria-label="Search in document" title="Search in document (⌘F / Ctrl+F)" onclick={searchDocument}><Search size={18} /></Button>{/if}
        <FeedbackIndicator />
        <DeferredPrivateView definition={deliverableAudio} label="audio controls" viewProps={{ sourceRef: shown.attachment.artifact_id, client: appDocumentAudioClient, rich: true }} />
        {#if revisions.length > 1}
          {#key shown.attachment.artifact_id}
            <RevisionSwitcher testid="artifact-revision" currentId={shown.attachment.artifact_id}
              label={`Revision ${revisions.find(revision => revision.artifact_id === shown.attachment.artifact_id)?.revision ?? 1} of ${Math.max(...revisions.map(revision => revision.revision))}`}
              load={async () => revisions.map(revision => ({ ...revision, id: revision.artifact_id }))}
              onSelect={revision => void showRevision(revision)} />
          {/key}
        {/if}
        <CompactHeaderActions>
          {#snippet children(menu: boolean)}
            {#if menu && searchable}<Button variant="ghost" size="inline" data-menu-close onclick={searchDocument}><Search size={18} />Search in document</Button>{/if}
            {#key shown.attachment.artifact_id}
            <AttachmentSaveButton filename={shown.attachment.filename} resolveUrl={downloadUrl} class="document-icon-action" label={menu ? 'Download' : ''} />
            {/key}
            <DocumentLinkActions internalUrl={`/library/artifact/${encodeURIComponent(shown.attachment.artifact_id)}`} createLink={$sharingPermission ? createPublicLink : undefined} defaultMode={viewEligible(shown.attachment.mime_type ?? '', shown.attachment.filename) ? 'view' : 'download'} shareLabel={menu ? 'Share link' : ''} />
          {/snippet}
        </CompactHeaderActions>
        <button bind:this={closeButton} class="inline-flex min-h-11 min-w-11 items-center justify-center rounded-lg text-foreground hover:bg-surface-hover" onclick={onClose} type="button" title="Close" aria-label="Close preview"><X class="h-5 w-5" /></button>
      </div>
    </header>
    <div class={`attachment-preview-content app-safe-fullscreen__content min-w-0 ${['html', 'pdf', 'video', 'audio'].includes(shown.kind) ? 'overflow-hidden' : 'overflow-auto p-3'}`}>
       <FeedbackWorkspace>
      {#if loading}
        <p class="text-sm text-muted">Loading preview…</p>
      {:else if error}
        <p class="text-sm text-danger">{error}</p>
      {:else}
        {#key shown.attachment.artifact_id}
        <ArtifactPreviewContent kind={shown.kind} filename={shown.attachment.filename} mime={shown.attachment.mime_type} {content} {richPayload} {truncated} mediaUrl={shown.mediaUrl} htmlSource={shown.htmlSource} />
        {/key}
      {/if}
       </FeedbackWorkspace>
    </div>
</div>
</FeedbackHost>

<style>
  .attachment-preview { padding-inline: 0; }
  .attachment-preview-content {
    margin-left: var(--app-overlay-inner-left, 0px);
    margin-right: var(--app-overlay-inner-right, 0px);
  }
  .attachment-preview-header {
    width: 100%;
    padding-left: max(.5rem, var(--app-overlay-inner-left, 0px));
    padding-right: max(.5rem, var(--app-overlay-inner-right, 0px));
  }
</style>
