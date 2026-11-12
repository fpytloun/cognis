<script lang="ts">
  import { onMount, tick, type Snippet } from 'svelte';
  import { toStore } from 'svelte/store';
  import { FeedbackPresenterSession, registerPresenterAdapter } from '$lib/feedback/presenter-session';
  import { surfaceIdentity } from '$lib/feedback/annotations/surface-identity';
  import type { FeedbackPresentationPort, FeedbackPresentationState } from '$lib/feedback/presentation';
  import FeedbackInline from './FeedbackInline.svelte';
  import FeedbackComments from './FeedbackComments.svelte';
  import { PublicFeedbackClient, type PublicFeedbackAccess, type PublicFeedbackContent } from '$lib/feedback/public-api';
  import { readPublicFeedbackSession, savePublicFeedbackSession, clearPublicFeedbackSession } from '$lib/feedback/public-session';
  import { GuestFeedbackDrafts, type PendingGuestComment } from '$lib/feedback/guest-drafts';
  import PublicSourcePreview from './PublicSourcePreview.svelte';
  import { createScopedTextResolver } from '$lib/feedback/scoped-text';
  import type { FeedbackAnchorCandidate, FeedbackMode, FeedbackSurfaceBinding, FeedbackThread, ResolvedFeedbackSource, FeedbackAnnotationAdapter } from '$lib/feedback/types';
  import FeedbackSourceProvider from './FeedbackSourceProvider.svelte';
  let { shareToken = '', capability = '', contentRef = '', children, onExchange }: {
    shareToken?: string; capability?: string; contentRef?: string; children?: Snippet; onExchange?: () => void;
  } = $props();
  const client = new PublicFeedbackClient();
  const presenter = new FeedbackPresenterSession();
  const presentation = presenter.state;
  let access = $state<PublicFeedbackAccess | null>(null);
  let preview = $state<PublicFeedbackContent | null>(null);
  let mediaUrl = $state<string | null>(null);
  const lifetime = new AbortController();
  const guard = { signal: lifetime.signal,
    assertCurrent() { if (lifetime.signal.aborted || !access) throw new Error('Public view is no longer available.'); },
    async validate() { this.assertCurrent(); await client.resolve({ kind: 'document', content_ref: access!.source.content_ref }); this.assertCurrent(); },
  };
  let threads = $state<FeedbackThread[]>([]);
  let next = $state<string | null>(null);
  let opened = $state(false);
  let view = $state<FeedbackPresentationState['view']>('threads');
  let mode = $state<FeedbackMode>('view');
  let candidate = $state<FeedbackAnchorCandidate | null>(null);
  let selected = $state<string | null>(null);
  let body = $state('');
  let displayName = $state('');
  let joined = $state(false);
  let busy = $state(false);
  let error = $state('');
  let generation = 0;
  let drafts: GuestFeedbackDrafts | null = null;
  let draftReady = $state(false);
  let pendingComment = $state<PendingGuestComment | null>(null);
  let outcomes = $state<{ entry_version_id: string; thread_id: string; disposition: 'addressed' | 'needs_clarification' | 'not_changed'; reason: string }[]>([]);
  let reply = $state('');
  let replyThreadId = $state<string | null>(null);
  function draftInput() { return { body, locator: $state.snapshot(candidate?.locator ?? null), reply, reply_thread_id: replyThreadId }; }
  function restoreDraft() {
    if (!access || !client.guest) return;
    drafts = new GuestFeedbackDrafts(access.share_id, client.guest.guest_session_id, access.source.content_digest);
    const value = drafts.read();
    if (value) {
      body = value.body; candidate = value.locator ? { locator: value.locator } : null;
      reply = value.reply; replyThreadId = value.reply_thread_id; pendingComment = value.pending;
    }
    draftReady = true;
  }
  const adapters = new Map<string, FeedbackAnnotationAdapter>();
  let resolveText = $state<FeedbackSurfaceBinding['resolveTextScope'] | null>(null);
  const source = $derived.by((): ResolvedFeedbackSource | null => access ? {
    // Public share identity is a local render key, never sent to owner APIs.
    subject_id: access.share_id, source_token: access.source.source_token,
    source: { kind: 'document', content_ref: access.source.content_ref }, context: { kind: 'standalone' },
    content_digest: access.source.content_digest, lineage_id: null, revision: null, latest_content_ref: null,
    format: access.source.format, title: access.source.title, availability: 'available',
    canonical_text: access.source.canonical_text, permitted_locator_kinds: access.permitted_locator_kinds,
    capabilities: { read: true, comment: access.allow_comments && joined, moderate: false, dispatch: false, disclose: false },
  } : null);
  const binding = $derived.by((): FeedbackSurfaceBinding | null => source && resolveText ? {
    source, context: { kind: 'standalone' }, mode, completeness: 'available',
    supported_locator_kinds: source.permitted_locator_kinds, selected_thread_id: selected,
    tool: $presentation.tool, accepted_candidate: candidate,
    onTargetChange: value => { guard.assertCurrent(); if (mode !== 'view' && source.capabilities.comment && (!value || value.surface_id === surfaceIdentity(binding))) presenter.publish(value); },
    highlights: threads.map(thread => ({ thread_id: thread.thread_id, anchor_id: thread.anchor.anchor_id,
      locator: thread.anchor.locator, category: 'exact', label: thread.entries[0]?.body ?? 'Comment' })),
    resolveTextScope: resolveText,
    onAnchorCandidate: value => {
      guard.assertCurrent();
      if (mode === 'view' || !source?.capabilities.comment || !source.permitted_locator_kinds.includes(value.locator.kind)) return;
      if (!body && !pendingComment) candidate = value; presenter.compose();
    },
    onSelectThread: id => { selected = id; if (id) presenter.open('thread'); },
    onInteractionChange: () => undefined,
    registerAdapter: (id, adapter) => registerPresenterAdapter(adapters, presenter, id, adapter),
  } : null);
  function downloadFilename(content: PublicFeedbackContent, value: PublicFeedbackAccess): string {
    const title = content.filename || 'document';
    if (!value.source.content_ref.startsWith('dlv_')) return title;
    const extension = content.format === 'rich' ? '.html' : '.md';
    return title.endsWith(extension) ? title : title + extension;
  }
  async function refresh(more = false) {
    const current = generation;
    const page = await client.threads(more ? next ?? undefined : undefined);
    if (current !== generation) return;
    threads = more ? [...threads, ...page.items] : page.items; next = page.next_cursor;
    outcomes = (await client.outcomes()).items;
  }
  onMount(() => {
    const current = ++generation;
    const cached = readPublicFeedbackSession();
    const initialize = async (): Promise<PublicFeedbackAccess> => {
      if (shareToken) { clearPublicFeedbackSession(); return client.open(shareToken); }
      if (capability && contentRef) return client.adoptCapability(capability, { kind: 'document', content_ref: contentRef });
      if (capability) throw new Error('The public viewer is missing its exact source reference.');
      if (cached) {
        client.resume(cached.access, cached.guest);
        await client.resolve({ kind: 'document', content_ref: cached.access.source.content_ref });
        return cached.access;
      }
      throw new Error('Open a valid feedback link from the document owner.');
    };
    void initialize().then(async value => {
      if (current !== generation) return;
      access = value;
      if (cached?.access.share_id === value.share_id && cached.guest) client.resume(value, cached.guest);
      joined = Boolean(client.guest);
      savePublicFeedbackSession({ access: value, guest: client.guest });
      onExchange?.();
      const identity = { source: { kind: 'document' as const, content_ref: value.source.content_ref },
        context: { kind: 'standalone' as const }, content_digest: value.source.content_digest };
      resolveText = createScopedTextResolver(identity, () => current === generation && access?.share_id === value.share_id ? identity : null,
        request => client.text(request), () => { if (current !== generation || !access) throw new Error('Public source changed.'); });
      const content = await client.content();
      let bytes: Blob | null = null;
      if (content.bytes_path || value.link_mode === 'download') bytes = await client.bytes(value.link_mode === 'download');
      if (current !== generation) return;
      preview = content;
      if (bytes) mediaUrl = URL.createObjectURL(bytes);
      if (value.link_mode === 'download' && mediaUrl) {
        const anchor = document.createElement('a');
        anchor.href = mediaUrl; anchor.download = downloadFilename(content, value);
        document.body.append(anchor); anchor.click(); anchor.remove();
      }
      if (joined && client.guest) restoreDraft();
      if (value.discussion === 'shared' || joined) await refresh();
    }).catch(caught => { if (current === generation) error = caught instanceof Error ? caught.message : 'Feedback is unavailable.'; });
    const revalidate = async () => {
      if (!access) return;
      try { await guard.validate(); }
      catch (caught) { if (current === generation) { clearPublicFeedbackSession(); client.close(); lifetime.abort(); resolveText = null; access = null; preview = null; error = caught instanceof Error ? caught.message : 'Public link is no longer available.'; } }
    };
    window.addEventListener('focus', revalidate);
    return () => { generation++; presenter.invalidate(); lifetime.abort(); client.close(); adapters.clear(); if (mediaUrl) URL.revokeObjectURL(mediaUrl); window.removeEventListener('focus', revalidate); };
  });
  $effect(() => {
    const input = draftInput();
    if (access && joined && draftReady && drafts) {
      try { drafts.write(input); } catch (caught) { error = caught instanceof Error ? caught.message : 'Guest draft could not be retained.'; }
    }
  });
  async function join() {
    busy = true; error = '';
    try { await client.join(displayName || null); if (access) savePublicFeedbackSession({ access, guest: client.guest }); restoreDraft(); joined = true; await refresh(); await tick(); presenter.open('comments'); }
    catch (caught) { error = caught instanceof Error ? caught.message : 'Could not join comments.'; }
    finally { busy = false; }
  }
  async function save() {
    if (!source || (!body.trim() && !pendingComment) || busy || !draftReady || !drafts) return;
    const savedBody = body; const savedCandidate = $state.snapshot(candidate);
    busy = true; error = '';
    try {
      if (pendingComment) { await replayPending(); return; }
      let locator = savedCandidate?.locator ?? { kind: 'whole' as const };
      if (locator.kind === 'html' && locator.shapes.length && !locator.capture_id) {
        if (!savedCandidate?.capture) throw new Error('Confirm an observed screenshot before saving HTML geometry.');
        locator = { ...locator, capture_id: await client.capture(source.source_token, source.content_digest, savedCandidate.capture) };
      }
      guard.assertCurrent();
      drafts.write(draftInput());
      pendingComment = drafts.freeze({ kind: 'thread',
        anchor: { schema_version: 1, source_token: source.source_token, content_digest: source.content_digest, locator },
        body: savedBody, original_locator: savedCandidate?.locator ?? null });
      await replayPending();
    } catch (caught) { error = caught instanceof Error ? caught.message : 'Comment was not saved. Your draft is retained.'; }
    finally { busy = false; }
  }
  async function replayPending() {
    const pending = pendingComment;
    if (!pending || !drafts) return;
    await guard.validate();
    if (pending.kind === 'thread') { const saved = await client.create(pending.creation_key, pending.anchor, pending.body); selected = saved.thread_id; }
    else await client.reply(pending.thread_id, pending.expected_thread_version, pending.reply_to_entry_id, pending.body, pending.creation_key);
    guard.assertCurrent();
    if (pending.kind === 'thread' && body === pending.body
      && JSON.stringify(candidate?.locator ?? null) === JSON.stringify(pending.original_locator)) { body = ''; candidate = null; }
    if (pending.kind === 'reply' && replyThreadId === pending.thread_id && reply === pending.body) { reply = ''; replyThreadId = null; }
    drafts.complete(pending.creation_key, draftInput());
    pendingComment = null;
    await refresh();
  }
  async function saveReply(thread: FeedbackThread) {
    if (!drafts || !draftReady || busy || !reply.trim() || pendingComment) return;
    busy = true; error = '';
    try {
      drafts.write(draftInput());
      pendingComment = drafts.freeze({ kind: 'reply', thread_id: thread.thread_id,
        expected_thread_version: thread.version, reply_to_entry_id: thread.entries.at(-1)?.entry_id ?? null, body: reply });
      await replayPending();
    } catch (caught) { error = caught instanceof Error ? caught.message : 'Original reply is retained.'; }
    finally { busy = false; }
  }
  const presentationSnapshot = $derived<FeedbackPresentationState>({
    source, threads, mode, selected, candidate, draft: body, saving: busy || !draftReady, pending_body: pendingComment?.body ?? null, reply_draft: { thread_id: replyThreadId, body: reply },
    error, loading: !access && !error, next_cursor: next, open: opened, view,
  });
  const port: FeedbackPresentationPort = {
    state: toStore(() => presentationSnapshot), session: presenter, context: { kind: 'standalone' },
    binding: () => binding, accept: value => binding?.onAnchorCandidate(value),
    authority: () => { guard.assertCurrent(); return JSON.stringify([access?.share_id, client.guest?.guest_session_id ?? null]); },
    setMode: value => { presenter.close(false); mode = value; }, setDraft: value => { body = value; },
    setReplyDraft: (threadId, value) => {
      if (reply && replyThreadId !== threadId) { error = 'Finish or clear the draft reply on its original thread first.'; return; }
      replyThreadId = threadId; reply = value;
    },
    canEdit: entry => entry.author.is_self && !entry.delivery.length,
    discard: async () => { if (!pendingComment && !busy) { body = ''; candidate = null; presenter.close(); } },
    save, closeRail: () => { opened = false; presenter.close(); if (mode === 'comment') mode = 'view'; },
    commentWhole: () => { if (!joined) return; if (mode === 'view') mode = 'comment'; binding?.onAnchorCandidate({ locator: { kind: 'whole' } }); },
    openRail: value => { view = value ?? 'threads'; opened = true; presenter.open(view === 'threads' ? 'comments' : view); },
    select: id => { selected = id; if (id) presenter.open('thread'); },
    geometry: value => {
      for (const adapter of adapters.values()) { const geometry = adapter.targetGeometry?.(value.locator); if (geometry) return geometry; }
      return null;
    },
    reveal: async value => { for (const adapter of adapters.values()) { const result = await adapter.locate(value.locator); guard.assertCurrent(); if (result.status === 'exact') return; } },
    locate: async thread => { selected = thread.thread_id; await port.reveal({ locator: thread.anchor.locator }); },
    more: () => refresh(true),
    expose: async (value, visible) => {
      guard.assertCurrent();
      for (const adapter of adapters.values()) {
        if (!adapter.targetGeometry?.(value.locator)?.rects.length) continue;
        await adapter.exposeTarget?.(value.locator, visible); guard.assertCurrent(); return;
      }
    },
    reload: () => refresh(),
    reply: async (thread, value) => { replyThreadId = thread.thread_id; reply = value; await saveReply(thread); if (error) throw new Error(error); },
    edit: async (entry, value) => { try { await client.edit(entry, value); await refresh(); } catch (caught) { error = caught instanceof Error ? caught.message : 'Edit failed.'; throw caught; } },
  };
</script>
{#if access?.link_mode === 'download' && mediaUrl && preview}
  <p role="status" class="p-3 text-sm">Your download is ready. <a class="text-accent underline" href={mediaUrl} download={downloadFilename(preview, access)}>Download file</a></p>
{/if}
{#snippet guest()}
    <p class="text-sm text-muted">Guest identity is unverified. Comments never send instructions to an agent automatically.</p>
    {#if access}<p class="mt-2 text-xs text-muted">{access.discussion === 'own_only' ? 'Only your comments are visible to you.' : access.discussion === 'shared' ? 'Comments are shared with other visitors.' : 'Discussion visibility is controlled by the link owner.'}</p>{/if}
    {#if access?.allow_comments && !joined}
      <label class="mt-3 block text-sm">Display name (optional)<input class="mt-1 min-h-11 w-full rounded border border-separator bg-surface p-2" bind:value={displayName} maxlength="80" /></label>
      <button type="button" class="min-h-11 text-accent" disabled={busy} onclick={join}>Join as guest</button>
    {:else if !access?.allow_comments}<p class="mt-3 text-sm">Commenting is disabled for this link.</p>{/if}
    {#each outcomes as outcome}<p class="mt-2 text-sm">{outcome.disposition.replaceAll('_', ' ')}: {outcome.reason}</p>{/each}
{/snippet}
{#snippet panel()}<FeedbackComments {port} {guest} />{/snippet}
<FeedbackSourceProvider {binding} {panel} available={Boolean(access?.allow_comments)} layout={$presentation.layout} onBoundsChange={port.session.setBounds.bind(port.session)} open={Boolean($presentation.surface)} onOpen={() => port.openRail()} onClose={port.closeRail}
  onRequestCommentAccess={access?.allow_comments && !joined ? () => port.openRail() : undefined}
  railOpen={opened} onShow={() => { if (opened) { port.closeRail(); return; } port.openRail(); if (joined && mode === 'view') mode = 'comment'; }}
  onWhole={() => binding?.onAnchorCandidate({ locator: { kind: 'whole' } })}
  onResume={body || pendingComment || candidate ? () => presenter.compose() : undefined}
  onMode={value => { port.setMode(value); presenter.setTool(value === 'draw' ? 'draw' : value === 'area' ? 'area' : 'text'); }}
  onCommenting={enabled => { if (!joined && enabled) { port.openRail(); return; } port.setMode(enabled ? 'comment' : 'view'); }}>
  {#if error && !access}<p role="alert" class="p-3 text-sm text-danger">{error}</p>{/if}
  {#if preview && access}<PublicSourcePreview content={preview} {mediaUrl} {guard} contentRef={access.source.content_ref} {client} />
  {:else if !error}<p role="status" class="p-4 text-sm">Loading public document…</p>{/if}
</FeedbackSourceProvider>
{#if source}<FeedbackInline {port} comments={panel} />{/if}
<style>button, input { min-height: 44px; }</style>
