<script lang="ts">
  import FeedbackHost from '$lib/components/feedback/FeedbackHost.svelte';
  import FeedbackIndicator from '$lib/components/feedback/FeedbackIndicator.svelte';
  import FeedbackWorkspace from '$lib/components/feedback/FeedbackWorkspace.svelte';
  import { resolveHtmlPreviewUrl } from '$lib/attachments/html-preview-private';
  import type { SignedHtmlPreview } from '$lib/attachments/html-preview';
  import { documentCoordination, type DocumentLease } from '$lib/document-coordination';
  import { provideDocumentSharing } from '$lib/document-sharing-context';
  import { onMount } from 'svelte';
  import { beforeNavigate } from '$app/navigation';
  import { api } from '$lib/api/client';
  import { auth } from '$lib/stores/auth';
  import Button from '$lib/components/ui/Button.svelte';
  import AttachmentSaveButton from '$lib/components/AttachmentSaveButton.svelte';
  import CompactHeaderActions from '$lib/components/CompactHeaderActions.svelte';
  import RevisionSwitcher from '$lib/components/RevisionSwitcher.svelte';
  import DocumentLinkActions from '$lib/components/rich/DocumentLinkActions.svelte';
  import DocumentFavoriteButton from '$lib/components/DocumentFavoriteButton.svelte';
  import DeliverableAudio from '$lib/components/DeliverableAudio.svelte';
  import { appDocumentAudioClient } from '$lib/app-document-audio';
  import DocumentActions from '$lib/components/rich/DocumentActions.svelte';
  import TextDocument from '$lib/components/TextDocument.svelte';
  import ArtifactPreviewContent from '$lib/components/ArtifactPreviewContent.svelte';
  import { previewKind } from '$lib/attachments/preview';
  import DocumentAgents from './DocumentAgents.svelte';
  import DocumentConversation from './DocumentConversation.svelte';
  import DocumentSidebar from './DocumentSidebar.svelte';
  import { isCompactViewport } from '$lib/stores/viewport';
  import Eye from 'lucide-svelte/icons/eye';
  import Pencil from 'lucide-svelte/icons/pencil';
  import Info from 'lucide-svelte/icons/info';
  import X from 'lucide-svelte/icons/x';
  import Search from 'lucide-svelte/icons/search';
  import { requestFind } from '$lib/find';
  import { syntaxError, viewEligible } from '$lib/artifact-text';
  import { deliverablePresentation } from '$lib/deliverable-presentation';
  import { privateDeliverableMediaUrl } from '$lib/rich-deliverable';
  import { formatLabel, libraryHref, originLabel, type LibraryItem } from '$lib/library';
  import type { Deliverable } from '$lib/types/api';
  import type { ShareLinkOptions } from '$lib/components/rich/share-options';
  import '$lib/components/rich/document-actions.css';

  let { item, onRevision, onDirtyChange, onBusyChange, onClose }: {
    item: LibraryItem; onRevision?: (item: LibraryItem) => void;
    onDirtyChange?: (dirty: boolean) => void; onBusyChange?: (busy: boolean) => void;
    onClose?: () => void;
  } = $props();
  let mediaUrl = $state('');
  let htmlSource = $state<SignedHtmlPreview | null>(null);
  let editHtmlLease = $state<DocumentLease | null>(null);
  let text = $state('');
  let truncated = $state(false);
  let richPayload = $state<unknown>(null);
  let error = $state('');
  let loading = $state(false);
  let revisions = $state<{ id: string; revision: number; is_latest: boolean; created_at: string | null; author_email?: string | null; author_agent_id?: string | null }[]>([]);
  let deliverable = $state<Deliverable | null>(null);
  let RichView = $state<typeof import('$lib/components/rich/RichDeliverable.svelte')['default'] | null>(null);
  let editing = $state(false);
  let preview = $state(false);
  let displayed = $state('');
  let original = $state('');
  let filename = $state('');
  let busy = $state(false);
  let feedbackBusy = $state(false);
  let detailsOpen = $state(false);
  let reader = $state<HTMLElement>();
  function searchDocument(event?: MouseEvent) {
    const details = event?.currentTarget instanceof Element ? event.currentTarget.closest('details') : null;
    if (details) { details.open = false; details.querySelector('summary')?.focus(); }
    const scope = reader?.querySelector<HTMLElement>('[data-find-scope]');
    if (scope) requestFind(scope);
  }
  const isImage = $derived(item.format.startsWith('image/'));
  const kind = $derived(previewKind({ filename: item.title ?? '', mime_type: item.format }));
  const isHTML = $derived(kind === 'html');
  const isText = $derived(['text', 'markdown', 'csv', 'mermaid'].includes(kind ?? ''));
  const searchable = $derived(!loading && !editing && (item.kind === 'deliverable' || isText || ['html', 'pdf'].includes(kind ?? '')));
  const canMutate = $derived(!item.is_readonly && $auth.user?.role !== 'viewer');
  provideDocumentSharing({
    get contentRef() { return item.kind === 'artifact' || item.kind === 'deliverable' ? item.id : null; },
    get canManage() { return canMutate && !!$auth.user?.email; },
    capture: () => documentCoordination().capture(),
    loadPeople: () => import('../rich/DocumentPeopleAccess.svelte'),
  });
  const canEdit = $derived(canMutate && item.kind === 'artifact' && (item.size_bytes == null || item.size_bytes <= 1024 * 1024) && (isText || isHTML));
  const dirty = $derived(editing && (displayed !== original || filename !== item.title));
  const validation = $derived(editing ? syntaxError(displayed, item.format, filename) : '');
  const downloadBase = $derived(`/api/v1/deliverables/${encodeURIComponent(item.id)}`);
  $effect(() => { onDirtyChange?.(dirty); onBusyChange?.(busy || feedbackBusy); });
  $effect(() => { if (!editing) displayed = text; });
  beforeNavigate((navigation) => {
    if (busy || (dirty && !confirm('Discard unsaved artifact changes?')) || (feedbackBusy && !confirm('Leave this revision? Its private feedback draft is saved separately.'))) navigation.cancel();
  });
  onMount(() => { detailsOpen = matchMedia('(min-width: 1024px)').matches; });

  async function startEdit() {
    busy = true; error = '';
    try {
      const lease = isHTML ? documentCoordination().capture() : null;
      const result = await api.artifactDocuments.source(item.id);
      await lease?.validate();
      editHtmlLease = lease;
      displayed = result.content; original = result.content; filename = result.filename;
      editing = true; preview = false;
    } catch (caught) { error = caught instanceof Error ? caught.message : 'Could not load editable source.'; }
    finally { busy = false; }
  }
  async function saveEdit() {
    if (validation) return;
    busy = true; error = '';
    try {
      const created = await api.artifactDocuments.revise(item.id, { filename, mime_type: item.format, content: displayed });
      editing = false; preview = false; busy = false;
      if (onRevision) onRevision(created); else item = created;
    } catch (caught) { error = caught instanceof Error ? caught.message : 'Could not save. Your draft is preserved.'; }
    finally { busy = false; }
  }
  async function selectRevision(id: string) {
    if (!id || id === item.id || busy) return;
    busy = true; error = '';
    try {
      const selected = await api.library.item(item.kind, id);
      if (onRevision) onRevision(selected); else item = selected;
    } catch (caught) { error = caught instanceof Error ? caught.message : 'Could not open that revision.'; }
    finally { busy = false; }
  }
  async function createPublicLink(options: ShareLinkOptions): Promise<string> {
    return (await api.artifactDocuments.publicLink(item.kind, item.id, { ...options, mode: options.mode ?? 'view' })).url;
  }

  $effect(() => {
    const current = item;
    const controller = new AbortController();
    let cancelled = false;
    error = ''; mediaUrl = ''; htmlSource = null; text = ''; richPayload = null; deliverable = null; revisions = [];
    editing = false; preview = false; loading = true;
    if (current.kind === 'deliverable') {
      void Promise.all([
        api.library.content(current.id), api.deliverables.revisions(current.id).catch(() => {
          if (!cancelled) error = 'Revision history is unavailable. You can still read this document.';
          return { revisions: [] };
        }),
        import('$lib/components/rich/RichDeliverable.svelte'),
      ]).then(([value, history, module]) => {
        if (cancelled) return;
        deliverable = value; RichView = module.default;
        revisions = history.revisions.map(r => ({ ...r, id: r.deliverable_id }));
      }).catch(caught => { if (!cancelled) error = caught instanceof Error ? caught.message : 'Could not load deliverable.'; })
        .finally(() => { if (!cancelled) loading = false; });
    } else {
      const currentKind = previewKind({ filename: current.title ?? '', mime_type: current.format });
      const textual = ['text', 'markdown', 'csv', 'mermaid'].includes(currentKind ?? '');
      void Promise.all([
        currentKind === 'html'
          ? resolveHtmlPreviewUrl(current.id, controller.signal)
          : api.artifacts.signedUrl(current.id, 3600, currentKind === 'pdf' ? 'view' : 'download'),
        textual ? api.artifacts.textPreview(current.id) : Promise.resolve(null),
        api.artifacts.revisions(current.id).catch(() => {
          if (!cancelled) error = 'Revision history is unavailable. You can still read this document.';
          return { revisions: [] };
        }),
      ]).then(([url, content, history]) => {
        if (cancelled) return;
        htmlSource = 'lease' in url ? url as SignedHtmlPreview : null;
        mediaUrl = url.url; text = content?.content ?? ''; truncated = content?.truncated ?? false;
        richPayload = content?.rich_payload ?? null;
        revisions = history.revisions.map(r => ({ ...r, id: r.artifact_id }));
      }).catch(caught => { if (!cancelled) error = caught instanceof Error ? caught.message : 'Could not load file.'; })
        .finally(() => { if (!cancelled) loading = false; });
    }
    return () => { cancelled = true; controller.abort(); };
  });
</script>

{#snippet revisionControls()}
  {#if revisions.length > 1}
    {#key item.id}
      <RevisionSwitcher testid="library-revision" currentId={item.id}
        label={`Revision ${revisions.find(revision => revision.id === item.id)?.revision ?? item.revision ?? 1} of ${Math.max(...revisions.map(revision => revision.revision))}`}
        load={async () => revisions}
        onSelect={revision => void selectRevision(revision.id)} />
    {/key}
  {/if}
{/snippet}

<svelte:window onbeforeunload={(event) => { if (dirty || busy || feedbackBusy) { event.preventDefault(); event.returnValue = ''; } }} />

<FeedbackHost source={{ kind: 'document', content_ref: item.id }} onBusyChange={(value) => { feedbackBusy = value; }}>
<section bind:this={reader} class="resource-reader flex h-full min-h-0 flex-col bg-canvas" data-testid="resource-reader" data-find-container>
  <!-- One row: controls keep their size; the title scrolls sideways instead of wrapping them. -->
  <header data-find-toolbar class:deliverable-controls={item.kind === 'deliverable'} class="flex shrink-0 items-center gap-2 border-b border-separator px-3 py-2.5 sm:gap-3 sm:px-5 sm:py-3">
    <div class="min-w-0 flex-1">
      <div class="flex min-w-0 items-center gap-2">
        <div class="scrollbar-hidden-x min-w-0">
          <h2 class="whitespace-nowrap text-sm font-semibold text-foreground" title={item.title ?? ''}>{item.title || 'Untitled document'}</h2>
          <p class="mt-0.5 whitespace-nowrap text-xs text-quiet">{formatLabel(item.format)} · Revision {item.revision ?? 1}{editing ? ' · Editing' : ''}</p>
        </div>
        {#if item.kind !== 'deliverable' && !$isCompactViewport}<DocumentFavoriteButton kind={item.kind} id={item.id} initial={item} />{/if}
      </div>
    </div>
    {#if item.kind === 'deliverable'}
      <DocumentActions
        onSearch={searchable ? searchDocument : undefined}
        internalUrl={libraryHref(item)}
        content={deliverable ? deliverable.content || JSON.stringify(deliverablePresentation(deliverable), null, 2) : ''}
        copyDisabled={loading || !deliverable}
        pdfUrl={`${downloadBase}/download.pdf`} htmlUrl={`${downloadBase}/download.html`}
        markdownUrl={['rich', 'markdown'].includes(item.format) ? `${downloadBase}/download.md` : ''}
        {revisionControls}
        createLink={canMutate ? createPublicLink : undefined}>
        {#snippet audioControls()}<FeedbackIndicator /><DeliverableAudio sourceRef={item.id} client={appDocumentAudioClient} rich />{/snippet}
        {#snippet extraActions(showLabel = false)}<DocumentFavoriteButton kind={item.kind} id={item.id} initial={item} {showLabel} />{/snippet}
        {#snippet menuActions()}
          <!-- Choosing details closes the More menu so it cannot cover the opened panel. -->
          <span class="contents" data-menu-close>
          <button type="button" aria-pressed={detailsOpen} onclick={() => { detailsOpen = !detailsOpen; }}><Info size={18} />{detailsOpen ? 'Hide details' : 'Show details'}</button>
          </span>
        {/snippet}
      </DocumentActions>
      {#if onClose}<button class="document-icon-action shrink-0" aria-label="Close resource" title="Close" disabled={busy} onclick={onClose}><X size={18} /></button>{/if}
    {:else}
    <nav data-find-actions class="flex shrink-0 items-center gap-1.5" aria-label="Document actions">
      {#if !editing}<FeedbackIndicator />{/if}
      {#if !editing}<DeliverableAudio sourceRef={item.id} client={appDocumentAudioClient} rich />{/if}
      {#if editing}
        <button class="document-icon-action" aria-label="Preview artifact" title={preview ? 'Return to source' : 'Preview artifact'} aria-pressed={preview} onclick={() => { preview = !preview; }} disabled={busy}><Eye size={18} /></button>
        <Button variant="ghost" onclick={() => { editing = false; preview = false; }} disabled={busy}>Cancel</Button>
        <Button onclick={saveEdit} disabled={busy || !!validation || !filename.trim() || !dirty}>{busy ? 'Saving…' : 'Save changes'}</Button>
      {:else}
        {@render revisionControls()}
        {#if searchable}<Button variant="ghost" size="icon" class="document-icon-action" aria-label="Search in document" title="Search in document (⌘F / Ctrl+F)" onclick={searchDocument}><Search size={18} /></Button>{/if}
        {#if canEdit && !$isCompactViewport}<Button variant="ghost" size="icon" class="document-icon-action" aria-label="Edit" title="Edit" disabled={busy || loading} onclick={startEdit}><Pencil size={18} /></Button>{/if}
        <CompactHeaderActions>
          {#snippet children(menu: boolean)}
            {#if menu && searchable}<Button variant="ghost" size="inline" data-menu-close onclick={searchDocument}><Search size={18} />Search in document</Button>{/if}
            <!-- Phones fold edit and favourite into the menu so the title keeps its room. -->
            {#if menu && canEdit}<Button variant="ghost" size="inline" disabled={busy || loading} onclick={startEdit}><Pencil size={18} />Edit</Button>{/if}
            {#if menu}<DocumentFavoriteButton kind={item.kind} id={item.id} initial={item} showLabel />{/if}
            <AttachmentSaveButton filename={item.title || 'artifact'} resolveUrl={async () => (await api.artifacts.signedUrl(item.id, 3600, 'download')).url} class="document-icon-action" label={menu ? 'Download' : ''} />
            <DocumentLinkActions internalUrl={libraryHref(item)} createLink={canMutate ? createPublicLink : undefined} defaultMode={viewEligible(item.format, item.title ?? '') ? 'view' : 'download'} shareLabel={menu ? 'Share link' : ''} />
          {/snippet}
        </CompactHeaderActions>
      {/if}
      <button class="document-icon-action" aria-label={detailsOpen ? 'Hide details' : 'Show details'} title="Details" aria-pressed={detailsOpen} onclick={() => { detailsOpen = !detailsOpen; }}><Info size={18} /></button>
      {#if onClose}<button class="document-icon-action" aria-label="Close resource" title="Close" disabled={busy} onclick={onClose}><X size={18} /></button>{/if}
    </nav>
    {/if}
  </header>
  {#if editing}
    <div class="shrink-0 border-b border-separator px-4 py-3">
      <label class="flex items-center gap-3 text-xs text-muted">File name
        <input aria-label="Artifact name" bind:value={filename} disabled={busy} class="min-h-10 min-w-0 flex-1 rounded-lg border border-control-border bg-surface px-3 text-sm text-foreground focus:border-accent focus:outline-none" />
      </label>
    </div>
  {/if}
  {#if validation || error}<p role="alert" class="shrink-0 border-b border-separator px-5 py-3 text-sm text-danger">{validation || error}</p>{/if}
  <div class="relative flex min-h-0 flex-1">
    <main class="min-h-0 min-w-0 flex-1 overflow-auto" aria-label="Document preview">
      <FeedbackWorkspace>
      {#if loading}<p role="status" class="p-6 text-sm text-muted">Loading document…</p>
      {:else if item.kind === 'deliverable' && deliverable && RichView}
        <RichView payload={deliverablePresentation(deliverable)} content={deliverable.content ?? ''} title={deliverable.title ?? ''} instanceId={deliverable.deliverable_id} mediaUrlFor={(key) => privateDeliverableMediaUrl(item.id, key)} surface="embedded" hosted conversational />
      {:else if item.kind === 'artifact'}
        {#if editing || (isText && !isHTML)}
          <TextDocument bind:text={displayed} mime={item.format} filename={editing ? filename : item.title ?? ''} {editing} {preview} {truncated} {richPayload} htmlLease={editHtmlLease} fill />
        {:else}
          <ArtifactPreviewContent kind={isImage ? 'image' : kind} filename={item.title ?? ''} mime={item.format} {mediaUrl} {htmlSource} />
        {/if}
      {/if}
      </FeedbackWorkspace>
    </main>
    <DocumentSidebar bind:open={detailsOpen}>
      {#snippet details()}
        <DocumentAgents {item} canManage={canMutate} />
        <dl class="space-y-4 text-xs">
          <div><dt class="text-quiet">Source</dt><dd class="mt-1 text-secondary">{originLabel(item.origin)}</dd></div>
          <div><dt class="text-quiet">Project</dt><dd class="mt-1 text-secondary">{#if item.project_id}<a class="text-accent hover:underline" href={`/projects/${encodeURIComponent(item.project_id)}`}>{item.project_name || 'Project'}</a>{:else}No project{/if}</dd></div>
          <div><dt class="text-quiet">Conversation</dt><dd class="mt-1 break-words text-secondary">{#if item.conversation_id}<DocumentConversation {item} />{:else}Source unavailable{/if}</dd></div>
          {#if item.task_id}<div><dt class="text-quiet">Task</dt><dd class="mt-1 break-words"><a class="text-accent hover:underline" href={`/tasks?task_id=${encodeURIComponent(item.task_id)}`}>{item.task_title || 'Open task'}</a></dd></div>{/if}
          <div><dt class="text-quiet">Format</dt><dd class="mt-1 text-secondary">{formatLabel(item.format)}{#if item.size_bytes != null} · {new Intl.NumberFormat(undefined, { notation: 'compact' }).format(item.size_bytes)} B{/if}</dd></div>
          <div><dt class="text-quiet">Created</dt><dd class="mt-1 text-secondary">{new Date(item.created_at).toLocaleString()}</dd></div>
          <div><dt class="text-quiet">Updated</dt><dd class="mt-1 text-secondary">{new Date(item.updated_at).toLocaleString()}</dd></div>
        </dl>
      {/snippet}
    </DocumentSidebar>
  </div>
</section>
</FeedbackHost>
<style>
  .deliverable-controls { --rich-action-size: 32px; --rich-action-icon-size: 18px; }
  @media (pointer: coarse) { .deliverable-controls { --rich-action-size: 44px; --rich-action-icon-size: 20px; } }
</style>
