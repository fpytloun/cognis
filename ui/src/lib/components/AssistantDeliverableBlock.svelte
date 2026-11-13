<script lang="ts">
  import { getContext, onDestroy } from 'svelte';
  import FeedbackHost from '$lib/components/feedback/FeedbackHost.svelte';
  import DocumentCommentCount from './DocumentCommentCount.svelte';
  import ChatFeedbackIntent from '$lib/components/chat-v2/ChatFeedbackIntent.svelte';
  import { readable, type Readable } from 'svelte/store';
  import { DELIVERABLE_SHARING_CONTEXT } from '$lib/deliverable-permissions';
  import { api } from '$lib/api/client';
  import { loadConversationDeliverable, peekConversationDeliverable, rememberConversationDeliverable } from '$lib/conversation-deliverables';
  import RichDeliverable from '$lib/components/rich/RichDeliverable.svelte';
  import DeliverableRevisionBar from '$lib/components/DeliverableRevisionBar.svelte';
  import {
    isSupersededRevision,
    loadDeliverableRevision,
    observedLineageRevisions,
    observeLineageRevision,
  } from '$lib/deliverable-revisions';
  import { deliverablePresentation } from '$lib/deliverable-presentation';
  import { privateDeliverableMediaUrl } from '$lib/rich-deliverable';
  import type { AssistantDeliverableTimelineItem as RenderAssistantDeliverableTimelineItem } from '$lib/timeline-render-model';
  import type { AssistantDeliverableTimelineItem } from '$lib/chat-v2/types';
  import type { Agent, Deliverable, DeliverableRevisionSummary, TaskDeliverableFreshness } from '$lib/types/api';
  import AgentAvatar from './AgentAvatar.svelte';
  import { now as nowStore } from '$lib/stores/now';
  import { formatAbsoluteTime, formatCompactTime } from '$lib/time';

  import DocumentFavoriteButton from './DocumentFavoriteButton.svelte';
  import DeferredPrivateView from './ui/DeferredPrivateView.svelte';
  import { deliverableAudio } from './ui/private-view-definitions';
  import { appDocumentAudioClient } from '$lib/app-document-audio';
  import { portal } from '$lib/actions/portal';
  import { isTopOverlay, registerOverlay } from '$lib/stores/overlays';
  export let item: AssistantDeliverableTimelineItem | RenderAssistantDeliverableTimelineItem;
  export let fullscreenOnly = false;
  export let onClose: (() => void) | undefined = undefined;
  export let conversationId = '';
  export let loadDeliverable: ((deliverableId: string) => Promise<Deliverable>) | undefined = undefined;
  export let loadRevision: ((deliverableId: string) => Promise<Deliverable>) | undefined = undefined;
  export let showLibraryLink = true;
  export let displayOnly = false;
  export let collapsedByDefault = false;
  export let inlineDisclosure = false;
  export let taskFreshness: TaskDeliverableFreshness | null = null;
  export let allowSharing = true;
  export let conversational = true;
  export let agent: Agent | null = null;
  export let showMessageIdentity = false;
  const sharingPermission = getContext<Readable<boolean>>(DELIVERABLE_SHARING_CONTEXT) ?? readable(true);

  // `baseDeliverable` is the revision this card represents; `revisionView` is a
  // revision the user opened from its history, rendered in place.
  let baseDeliverable: Deliverable | null = null;
  let revisionView: Deliverable | null = null;
  let revisionError: string | null = null;
  let viewerFullscreen = false;
  let fullscreenReturnFocus: HTMLElement | null = null;
  let revisionGeneration = 0;
  let deliverable: Deliverable | null = null;
  let feedbackBusy = false;
  $: deliverable = revisionView ?? baseDeliverable;
  $: supersededCollapsed = !feedbackBusy && !revisionView && isSupersededRevision(baseDeliverable);
  $: effectiveTaskFreshness = taskFreshness ?? baseDeliverable?.task_freshness ?? null;
  $: taskFreshnessCollapsed = !feedbackBusy && !revisionView && effectiveTaskFreshness !== null && effectiveTaskFreshness.state !== 'current';
  let loading = true;
  let error: string | null = null;
  let accessorConversationId = '';
  let loadGeneration = 0;
  onDestroy(() => { loadGeneration += 1; revisionGeneration += 1; });
  $: deliverableId = 'deliverable_id' in item ? item.deliverable_id : item.deliverableId;

  $: deliverableTitle = deliverable?.title ?? item.title ?? 'Deliverable';
  $: agentName = agent ? (agent.display_name ?? agent.name) : 'Assistant';
  $: messageTimestamp = ('deliverable_id' in item ? item.created_at : item.createdAt) ?? baseDeliverable?.created_at ?? null;
  $: nowDate = new Date($nowStore);
  $: standaloneUrl = deliverable ? `/api/v1/deliverables/${encodeURIComponent(deliverable.deliverable_id)}/view` : '';
  $: pdfUrl = deliverable ? `/api/v1/deliverables/${encodeURIComponent(deliverable.deliverable_id)}/download.pdf` : '';
  $: htmlUrl = deliverable ? `/api/v1/deliverables/${encodeURIComponent(deliverable.deliverable_id)}/download.html` : '';
  $: markdownUrl = deliverable && ['rich', 'markdown'].includes(deliverable.format)
    ? `/api/v1/deliverables/${encodeURIComponent(deliverable.deliverable_id)}/download.md` : '';
  // Every deliverable format renders through RichDeliverable now, giving
  // markdown/plain/html the same unified toolbar (TOC, full-view, share,
  // copy, PDF) and chrome that rich payloads already have -- previously
  // these formats rendered through a separate, far more limited
  // `assistant-deliverable-card` with only "Open standalone page"/
  // "Download PDF"/"Copy share link" actions, no TOC beyond a flat
  // heading list, and no full-view. `rich` payloads pass through as-is;
  // every other format is wrapped as a single block so the existing block
  // renderers do the work: `markdown` already renders full markdown
  // (headings, tables, code with syntax highlighting, links) via
  // MarkdownBlock; `plain` uses the `code` block so literal text is never
  // misinterpreted as markdown; `html` uses the `raw_html` block, a
  // client-only synthetic type (see RawHtmlBlock.svelte) that sanitizes
  // and renders already-HTML content directly instead of running it
  // through the markdown parser, which would otherwise escape it.
  // A single wrapped `markdown` block only ever contributes ONE top-level
  // (level 2) TOC entry -- its own title, taken from the first heading --
  // with every other `#`/`##`/`###` heading nested one level deeper. The
  // shared `isSubstantialDocument()` auto-TOC heuristic gates on
  // `topLevelHeadings >= 4`, which a single block can never satisfy
  // regardless of how many internal headings it has, so a markdown
  // deliverable would silently lose the TOC the old bespoke card used to
  // show. Detect real heading count directly and force `metadata.toc`
  // on/off explicitly (the same threshold the old card used) rather than
  // relying on the generic multi-block heuristic here.
  $: richPayload = deliverable ? deliverablePresentation(deliverable) : null;

  function authenticatedMediaUrlFor(mediaKey: string): string {
    if (!deliverable) return '';
    return privateDeliverableMediaUrl(deliverable.deliverable_id, mediaKey, accessorConversationId);
  }

  function currentConversationId(): string {
    if (conversationId) return conversationId;
    const match = typeof window !== 'undefined' ? window.location.pathname.match(/\/chat\/([^/]+)/) : null;
    return match?.[1] ? decodeURIComponent(match[1]) : '';
  }

  function pendingDialog(node: HTMLElement) {
    const overlay = registerOverlay({ kind: 'fullscreen', blocksChrome: true });
    node.querySelector<HTMLButtonElement>('button')?.focus();
    const keydown = (event: KeyboardEvent) => {
      if (!isTopOverlay(overlay.id)) return;
      if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); onClose?.(); }
      if (event.key === 'Tab') { event.preventDefault(); node.querySelector<HTMLButtonElement>('button')?.focus(); }
    };
    node.addEventListener('keydown', keydown);
    return { destroy() {
      node.removeEventListener('keydown', keydown);
      overlay.unregister();
    } };
  }

  async function createShareLink(options?: import('$lib/components/rich/share-options').ShareLinkOptions): Promise<string> {
    if (!deliverable) throw new Error('Deliverable is not loaded');
    const result = await api.artifactDocuments.publicLink('deliverable', deliverable.deliverable_id, { ...options, mode: options?.mode ?? 'view' });
    return result.url;
  }

  async function loadCurrent(
    id: string,
    loaderOverride: ((deliverableId: string) => Promise<Deliverable>) | undefined,
    previewOnly: boolean,
  ): Promise<void> {
    const generation = ++loadGeneration;
    revisionGeneration += 1;
    loading = true;
    error = null;
    if (baseDeliverable?.deliverable_id !== id) baseDeliverable = null;
    revisionView = null;
    revisionError = null;
    accessorConversationId = currentConversationId();
    if (previewOnly && !loaderOverride) {
      loading = false;
      return;
    }
    const cached = !loaderOverride && peekConversationDeliverable(id, accessorConversationId);
    if (cached) {
      baseDeliverable = cached;
      loading = false;
      return;
    }
    try {
      const result = await (loaderOverride
        ? loaderOverride(id)
        : loadConversationDeliverable(id, accessorConversationId));
      if (generation === loadGeneration && deliverableId === id) baseDeliverable = result;
    } catch (err) {
      if (generation === loadGeneration && deliverableId === id) {
        error = err instanceof Error ? err.message : 'Failed to load deliverable';
      }
    } finally {
      if (generation === loadGeneration && deliverableId === id) loading = false;
    }
  }

  function resetInlineRevision(id: string): void {
    if (baseDeliverable?.deliverable_id !== id) {
      revisionGeneration += 1;
      revisionView = null;
      revisionError = null;
    }
  }

  $: if (!displayOnly && item.deliverable?.deliverable_id === deliverableId) {
    loadGeneration += 1;
    accessorConversationId = currentConversationId();
    // Reading baseDeliverable here would make its assignment self-reactive.
    resetInlineRevision(deliverableId);
    baseDeliverable = item.deliverable;
    if (!loadDeliverable) rememberConversationDeliverable(item.deliverable, accessorConversationId);
    error = null;
    loading = false;
  } else {
    void loadCurrent(deliverableId, loadDeliverable, displayOnly);
  }

  // Publish this card's revision, and refresh lineage facts when a newer
  // revision of the same lineage is rendered after this card loaded.
  $: itemLineageId = 'deliverable_id' in item ? item.lineage_id : item.lineageId;
  $: itemRevision = item.revision;
  $: observeLineageRevision(itemLineageId, itemRevision);
  $: observeLineageRevision(baseDeliverable?.lineage_id, baseDeliverable?.revision);
  $: lineageKey = itemLineageId ?? baseDeliverable?.lineage_id ?? null;
  $: observedLatest = lineageKey ? $observedLineageRevisions[lineageKey] ?? 0 : 0;
  let refreshedForRevision = 0;
  $: if (
    baseDeliverable
    && observedLatest > (baseDeliverable.latest_revision ?? baseDeliverable.revision ?? 1)
    && observedLatest > refreshedForRevision
  ) {
    refreshedForRevision = observedLatest;
    void refreshBase(baseDeliverable.deliverable_id);
  }

  async function refreshBase(id: string): Promise<void> {
    const generation = loadGeneration;
    try {
      const result = await (loadDeliverable
        ? loadDeliverable(id)
        : api.deliverables.get(id, { accessorConversationId }));
      if (generation === loadGeneration && deliverableId === id) {
        baseDeliverable = result;
        if (!loadDeliverable) rememberConversationDeliverable(result, accessorConversationId);
      }
    } catch {
      // Keep the loaded revision; lineage facts refresh on the next load.
    }
  }

  async function openRevision(revision: DeliverableRevisionSummary): Promise<void> {
    if (feedbackBusy) { revisionError = 'Finish or close feedback before switching revisions. Your private draft remains on its original revision.'; return; }
    const generation = ++revisionGeneration;
    revisionError = null;
    if (revision.deliverable_id === baseDeliverable?.deliverable_id) {
      revisionView = null;
      return;
    }
    try {
      const result = await (loadRevision
        ? loadRevision(revision.deliverable_id)
        : loadDeliverableRevision(revision, { accessorConversationId }));
      if (generation === revisionGeneration) revisionView = result;
    } catch (err) {
      if (generation === revisionGeneration) {
        revisionError = err instanceof Error ? err.message : 'Could not load revision';
      }
    }
  }
</script>

{#if fullscreenOnly && !deliverable}
  <div class="fullscreen-pending" role="dialog" aria-modal="true" aria-label={deliverableTitle} tabindex="-1" use:portal use:pendingDialog>
    {#if error}<p role="alert">Could not load deliverable: {error}</p>
    {:else}<p role="status">Loading deliverable…</p>{/if}
    <button type="button" on:click={onClose}>Close document</button>
  </div>
{:else}
<div class="assistant-deliverable-wrapper" class:conversational data-deliverable-id={deliverableId} data-collapsed-by-default={collapsedByDefault ? 'true' : undefined}>
  {#if !fullscreenOnly && !displayOnly}
    <div class="flex justify-end"><DocumentCommentCount id={deliverableId} /></div>
  {/if}
  {#if loading && !deliverable}
    <div class="flex min-h-12 items-center gap-2 text-sm text-muted" role="status" aria-live="polite" aria-busy="true">
      <svg class="h-4 w-4 shrink-0 animate-spin motion-reduce:animate-none" viewBox="0 0 24 24" fill="none" aria-hidden="true">
        <circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="2" opacity=".25" />
        <path d="M12 3a9 9 0 0 1 9 9" stroke="currentColor" stroke-width="2" stroke-linecap="round" />
      </svg>
      <span>Loading deliverable…</span>
    </div>
  {:else if displayOnly && !deliverable}
    <div class="rounded-xl border border-control-border bg-surface/60 p-3" data-testid="display-only-deliverable">
      <p class="text-sm font-medium text-foreground">{deliverableTitle}</p>
      {#if item.format}<p class="mt-1 text-xs text-quiet">{item.format} deliverable · preview only</p>{/if}
      {#if item.content && !collapsedByDefault}<p class="mt-2 line-clamp-6 whitespace-pre-wrap text-sm text-secondary">{item.content}</p>{/if}
    </div>
  {:else if error && !deliverable}
    <div class="space-y-2">
      <div class="text-sm font-medium text-danger">Could not load deliverable</div>
      <div class="text-xs text-quiet">{error}</div>
      {#if item.content}
        <div class="rounded-xl border border-control-border bg-surface/60 p-3 text-sm text-foreground">
          {item.content}
        </div>
      {/if}
    </div>
  {:else if deliverable && richPayload}
    {#if showLibraryLink && !displayOnly && !conversational}
      <div class="mb-2 flex justify-end"><a class="inline-flex min-h-9 items-center text-xs text-accent hover:text-accent-hover hover:underline" href={`/library/deliverable/${encodeURIComponent(deliverable.deliverable_id)}`}>Open in Library ↗</a></div>
    {/if}
    {#if !conversational}<DeliverableRevisionBar part="notice" current={deliverable} anchorId={baseDeliverable?.deliverable_id ?? deliverableId} onSelect={(revision) => void openRevision(revision)} />{/if}
    <!-- Remount when the card becomes superseded so it applies the collapsed default. -->
    {#if effectiveTaskFreshness && effectiveTaskFreshness.state !== 'current'}
      <p class="mb-2 text-xs text-muted" role="status">{effectiveTaskFreshness.label}</p>
    {/if}
    {#key `${deliverable.deliverable_id}:${supersededCollapsed}:${taskFreshnessCollapsed}`}
    <FeedbackHost source={{ kind: 'document', content_ref: deliverable.deliverable_id }} onBusyChange={(value) => { feedbackBusy = value; }}>
    <ChatFeedbackIntent sourceKey={deliverable.deliverable_id} document>
    <RichDeliverable
      bind:fullOpen={viewerFullscreen}
      bind:lastFocusedElement={fullscreenReturnFocus}
      title={deliverableTitle}
      {fullscreenOnly}
      onCloseFullscreen={onClose}
      content={deliverable.content}
      payload={richPayload}
       instanceId={deliverable.deliverable_id}
      internalUrl={conversational && (!showLibraryLink || displayOnly) ? '' : `/library/deliverable/${encodeURIComponent(deliverable.deliverable_id)}`}
      mediaUrlFor={authenticatedMediaUrlFor}
      standaloneUrl={standaloneUrl}
      pdfUrl={pdfUrl}
      htmlUrl={htmlUrl}
      markdownUrl={markdownUrl}
      shareLinkCallback={allowSharing && $sharingPermission ? createShareLink : undefined}
      surface="embedded"
      {conversational}
      compact
       collapsedByDefault={collapsedByDefault || supersededCollapsed || taskFreshnessCollapsed}
       inlineDisclosure={inlineDisclosure || (effectiveTaskFreshness !== null && effectiveTaskFreshness.state !== 'current')}
    >
      {#snippet extraActions(showLabel = false)}
        <DocumentFavoriteButton kind="deliverable" id={deliverable.deliverable_id} {showLabel} />
      {/snippet}
      {#snippet revisionControls()}
        <DeliverableRevisionBar part="switcher" current={deliverable} anchorId={baseDeliverable?.deliverable_id ?? deliverableId} onSelect={(revision) => void openRevision(revision)} />
        {#if revisionError}<span role="alert" class="text-xs text-danger">{revisionError}</span>{/if}
      {/snippet}
      {#snippet audioControls()}
        {#if !displayOnly}
          <DeferredPrivateView definition={deliverableAudio} label="audio controls" viewProps={{ sourceRef: deliverable.deliverable_id, client: appDocumentAudioClient, rich: deliverable.format === 'rich' }} />
        {/if}
      {/snippet}
      {#snippet footer()}
        {#if showMessageIdentity}
          <div class="flex min-w-0 items-center gap-1.5 text-[11px] opacity-80" data-testid="deliverable-message-identity">
            <AgentAvatar name={agentName} avatarUrl={agent?.avatar_url ?? null} class="h-4 w-4 rounded-md text-[9px]" />
            <span class="truncate font-medium text-foreground">{agentName}</span>
            <span class="text-quiet">·</span>
            <span class="whitespace-nowrap text-muted" title={formatAbsoluteTime(messageTimestamp)}>{formatCompactTime(messageTimestamp, nowDate)}</span>
          </div>
        {/if}
        <DeliverableRevisionBar part="notice" current={deliverable} anchorId={baseDeliverable?.deliverable_id ?? deliverableId} onSelect={(revision) => void openRevision(revision)} />
      {/snippet}
    </RichDeliverable>
    </ChatFeedbackIntent>
    </FeedbackHost>
    {/key}
  {/if}
</div>
{/if}

<style>
  .fullscreen-pending { position: fixed; inset: 0; z-index: 2147483000; display: grid; place-content: center; gap: 1rem; padding: 1.5rem; color: rgb(var(--color-foreground)); background: rgb(var(--color-canvas)); }
  .assistant-deliverable-wrapper {
    width: 100%;
    min-width: 0;
    max-width: 100%;
    margin: 0.75rem 0;
    /* RichDeliverable owns all its own document chrome now for every
       format; avoid a nested frame here. */
    padding: 0;
  }
  /* Match ChatMessage's avatar, gap, bubble padding and one-pixel border. */
  .assistant-deliverable-wrapper.conversational { box-sizing: border-box; padding-left: calc(3.25rem + 1px); }
  @media (min-width: 640px) {
    .assistant-deliverable-wrapper.conversational { padding-left: calc(3.75rem + 1px); }
  }
  /* Paper has no leading avatar; documents share the tool rows' inset (px-3) with its messages. */
  :global([data-theme-family='paper']) .assistant-deliverable-wrapper.conversational { padding-left: 0.75rem; }
</style>
