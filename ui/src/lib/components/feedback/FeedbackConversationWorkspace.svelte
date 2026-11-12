<script lang="ts">
  import { onMount, untrack } from 'svelte';
  import { get } from 'svelte/store';
  import type { CommentWorkspace } from '$lib/feedback/comment-workspace';
  import { FeedbackController } from '$lib/feedback/state';
  import { feedbackApi } from '$lib/feedback/api';
  import { documentCoordination } from '$lib/document-coordination';
  import type { FeedbackThread } from '$lib/feedback/types';
  import { targetSummary } from '$lib/feedback/target-summary';
  import { surfacePortal } from '$lib/feedback/surface-portal';
  import { feedbackOverlayBounds } from '$lib/feedback/overlay-bounds';
  import { highlightComments } from '$lib/feedback/locate-target';
  import FeedbackInline from './FeedbackInline.svelte';
  import FeedbackPanel from './FeedbackPanel.svelte';
  import TargetQuote from './TargetQuote.svelte';
  import CommentReplies from './CommentReplies.svelte';
  import CommentEntry from './CommentEntry.svelte';
  import FeedbackChromeMount from './FeedbackChromeMount.svelte';
  import { reviewChrome } from './review-chrome';
  import Button from '$lib/components/ui/Button.svelte';
  import StatusBadge from '$lib/components/ui/StatusBadge.svelte';
  import { commentStatus, sendableEntry } from '$lib/feedback/thread-status';
  import { conversationFeedback, type ConversationFeedback } from '$lib/feedback/staging';
  import type { FeedbackEntryVersion, FeedbackStagingSnapshot } from '$lib/feedback/types';
  import type { FeedbackReviewSource } from '$lib/feedback/review-selection';
  let { workspace, conversationId, viewport }: { workspace: CommentWorkspace; conversationId: string; viewport?: HTMLElement | null } = $props();
  const workspaceState = $derived(workspace.state);
  const lease = untrack(() => documentCoordination().capture());
  let fetched = $state<FeedbackThread[]>([]), loading = $state(false), error = $state(''), active = $state(false);
  let right = $state(8), bottom = $state(8), width = $state(384), maxHeight = $state(500);
  let alive = true, generation = 0, refreshing = false;
  const owned: { owner: FeedbackController; release: () => void }[] = [];
  const threads = $derived.by(() => {
    void $workspaceState.revision;
    const all = new Map(fetched.map(thread => [thread.thread_id, thread]));
    // A loaded viewer can hold an older copy; the higher version is always the current state.
    for (const owner of workspace.controllers()) {
      for (const thread of get(owner.state).threads) {
        const known = all.get(thread.thread_id);
        if (!known || thread.version > known.version) all.set(thread.thread_id, thread);
      }
    }
    return [...all.values()].filter(thread => !thread.hidden);
  });
  // The chat's one comment list also chooses what goes with the next message (the composer chip).
  let channel = $state.raw<ConversationFeedback | null>(null);
  let staging = $state.raw<FeedbackStagingSnapshot | null>(null);
  let including = $state(false);
  let handoff = $state(false);
  $effect(() => {
    let owner: ConversationFeedback;
    try { owner = conversationFeedback(conversationId); } catch { channel = null; return; }
    channel = owner;
    const stops = [owner.snapshot.subscribe(value => { staging = value; }), owner.busy.subscribe(value => { including = value; })];
    void owner.refresh();
    return () => { for (const stop of stops) stop(); };
  });
  const stagedRows = $derived(new Map((staging?.items ?? []).map(item => [item.entry_id, item])));
  /** The latest version of each staged human entry on an open thread. */
  const stagedEntries = $derived(threads.filter(thread => thread.state === 'open')
    .flatMap(thread => thread.entries).filter(entry => sendableEntry(entry) && stagedRows.has(entry.entry_id)));
  const includedEntries = $derived(stagedEntries.filter(entry => stagedRows.get(entry.entry_id)?.inclusion === 'included'));
  const sources = $derived(threads.flatMap(thread => thread.entries.map(entry => ({
    entry_version_id: entry.entry_version_id, version: entry.version, thread_id: thread.thread_id,
    anchor_id: thread.anchor.anchor_id, source: thread.anchor.source,
  } satisfies FeedbackReviewSource))));
  async function includeOnly(chosen: FeedbackEntryVersion[]) {
    if (!channel || including) return;
    error = '';
    try { await channel.setIncluded(chosen, sources, lease); }
    catch (caught) { if (alive && !lease.signal.aborted) error = caught instanceof Error ? caught.message : 'The comments were not included. Try again.'; }
  }
  function toggleIncluded(entry: FeedbackEntryVersion, on: boolean) {
    void includeOnly(on ? [...includedEntries, entry] : includedEntries.filter(value => value.entry_id !== entry.entry_id));
  }
  onMount(() => {
    const stop = documentCoordination().onInvalidated(() => { generation++; fetched = []; workspace.closeComments(); });
    // Resolving or removing in Review comments (or elsewhere) refreshes this list.
    const changed = () => { if (get(workspace.state).list) void refresh(); };
    window.addEventListener('feedback-changed', changed);
    window.addEventListener('focus', changed);
    return () => { alive = false; generation++; stop(); window.removeEventListener('feedback-changed', changed); window.removeEventListener('focus', changed); for (const item of owned) { item.release(); item.owner.dispose(); } };
  });
  $effect(() => {
    if (!$workspaceState.list) return;
    // A different client can reply/resolve without a local event or an active agent turn.
    void untrack(refresh);
    const timer = window.setInterval(() => {
      if (document.visibilityState === 'visible' && !refreshing) void refresh(false);
    }, 5000);
    return () => window.clearInterval(timer);
  });
  $effect(() => {
    if (!$workspaceState.list || !active) return;
    const host = viewport;
    let frame = 0;
    function update() {
      const visual = window.visualViewport;
      const edges = { left: visual?.offsetLeft ?? 0, top: visual?.offsetTop ?? 0,
        right: (visual?.offsetLeft ?? 0) + (visual?.width ?? innerWidth),
        bottom: (visual?.offsetTop ?? 0) + (visual?.height ?? innerHeight) };
      const bounds = feedbackOverlayBounds(host?.getBoundingClientRect() ?? edges, edges);
      right = innerWidth - bounds.right + 8; bottom = innerHeight - bounds.bottom + 8;
      width = Math.max(0, Math.min(384, bounds.width - 16));
      maxHeight = Math.max(0, (bounds.height - 16) * .65);
    }
    function schedule() { if (!frame) frame = requestAnimationFrame(() => { frame = 0; update(); }); }
    update();
    const observer = new ResizeObserver(schedule);
    if (host) observer.observe(host);
    window.addEventListener('resize', schedule); window.addEventListener('scroll', schedule, true);
    window.visualViewport?.addEventListener('resize', schedule); window.visualViewport?.addEventListener('scroll', schedule);
    return () => {
      if (frame) cancelAnimationFrame(frame);
      observer.disconnect();
      window.removeEventListener('resize', schedule); window.removeEventListener('scroll', schedule, true);
      window.visualViewport?.removeEventListener('resize', schedule); window.visualViewport?.removeEventListener('scroll', schedule);
    };
  });
  async function refresh(showLoading = true) {
    const request = ++generation;
    refreshing = true;
    if (showLoading) loading = true;
    error = '';
    try {
      const staged = await feedbackApi.staging(conversationId);
      lease.assertCurrent();
      const result: FeedbackThread[] = [];
      // Include resolved comments: subject_ids keeps every commented source, not only ready ones.
      for (const subject of new Set([...(staged.subject_ids ?? []), ...staged.items.map(item => item.subject_id)])) {
        let cursor: string | undefined;
        do {
          const page = await feedbackApi.threads(subject, cursor, lease.signal);
          lease.assertCurrent();
          if (!alive || request !== generation) return;
          result.push(...page.items); cursor = page.next_cursor ?? undefined;
        } while (cursor);
      }
      if (alive && request === generation) fetched = result;
    } catch (caught) {
      if (alive && request === generation && !lease.signal.aborted) error = caught instanceof Error ? caught.message : 'Comments could not be loaded.';
    } finally { if (alive && request === generation) { loading = false; refreshing = false; } }
  }
  /** Jump to the commented text and emphasise it; the list stays open for the next one. */
  async function locate(thread: FeedbackThread) {
    error = '';
    const found = await workspace.locate(thread);
    if (!found && alive) error = 'The commented content is no longer in this conversation.';
  }
  let changing = $state('');
  let tab = $state<'open' | 'resolved'>('open');
  const openCount = $derived(threads.filter(thread => thread.state === 'open').length);
  const shown = $derived(threads.filter(thread => thread.state === tab));
  async function change(thread: FeedbackThread, action: 'resolve' | 'remove') {
    if (changing) return;
    changing = thread.thread_id; error = '';
    try {
      const updated = await feedbackApi.changeThread(thread, action === 'resolve' ? { state: thread.state === 'open' ? 'resolved' : 'open' } : { hidden: true });
      lease.assertCurrent();
      // Show the result immediately; the refresh below reconciles everything else.
      fetched = [...fetched.filter(item => item.thread_id !== updated.thread_id), updated];
      for (const owner of workspace.controllers()) if (JSON.stringify(owner.sourceRef) === JSON.stringify(thread.anchor.source)) void owner.load();
      window.dispatchEvent(new Event('feedback-changed'));
    } catch (caught) {
      if (alive && !lease.signal.aborted) error = caught instanceof Error ? caught.message : 'The comment could not be changed.';
    } finally { if (alive) changing = ''; }
  }
  async function reply(thread: FeedbackThread, body: string) {
    await feedbackApi.replyTo(thread, body, { kind: 'chat', conversation_id: conversationId });
    lease.assertCurrent();
    for (const owner of workspace.controllers()) if (JSON.stringify(owner.sourceRef) === JSON.stringify(thread.anchor.source)) void owner.load();
    window.dispatchEvent(new Event('feedback-changed'));
  }
  // Commented text stays highlighted while the list is open.
  $effect(() => {
    if (!$workspaceState.list || !threads.length) return;
    return highlightComments(threads.map(thread => ({ source: thread.anchor.source, locator: thread.anchor.locator })));
  });
</script>
<svelte:window onkeydown={event => { if ($workspaceState.list && event.key === 'Escape') { event.stopPropagation(); workspace.closeComments(); } }} />
{#if $workspaceState.list}
  <section use:surfacePortal={visible => active = visible} hidden={!active} inert={!active} aria-label="Comments" class="fixed z-[2147483103] flex flex-col overflow-hidden rounded-xl border border-separator bg-elevated text-sm text-foreground shadow-2xl"
    style:right={`${right}px`} style:bottom={`${bottom}px`} style:width={`${width}px`} style:max-height={`${maxHeight}px`}>
    <header class="flex shrink-0 items-center justify-between border-b border-separator px-3 py-1"><h2 class="font-semibold">Comments</h2><Button variant="ghost" size="icon" class="-mr-2 text-muted" aria-label="Close comments" onclick={() => workspace.closeComments()}>×</Button></header>
    <div class="min-h-0 overflow-y-auto p-2">
      {#if handoff}
        <FeedbackChromeMount definition={reviewChrome} viewProps={{
          entries: includedEntries, sources, subjectIds: [...new Set(threads.map(thread => thread.subject_id))],
          context: { kind: 'chat', conversation_id: conversationId }, handoff: true,
          initialSelection: includedEntries.map(entry => entry.entry_version_id), onClose: () => { handoff = false; },
        }} onClose={() => { handoff = false; }} />
      {:else}
      {#if loading && !threads.length}<p role="status" class="p-2 text-muted">Loading comments…</p>{/if}
      {#if error}<p role="alert" class="p-2 text-danger">{error}</p><Button variant="ghost" size="sm" class="text-accent" onclick={() => refresh()}>Retry</Button>{/if}
      <div class="mb-1 inline-flex rounded-md bg-surface p-0.5 text-xs" role="tablist" aria-label="Comment state">
        <button type="button" role="tab" aria-selected={tab === 'open'} class="rounded px-2 py-1" class:bg-elevated={tab === 'open'} class:text-muted={tab !== 'open'} onclick={() => tab = 'open'}>Open {openCount}</button>
        <button type="button" role="tab" aria-selected={tab === 'resolved'} class="rounded px-2 py-1" class:bg-elevated={tab === 'resolved'} class:text-muted={tab !== 'resolved'} onclick={() => tab = 'resolved'}>Resolved {threads.length - openCount}</button>
      </div>
      {#if tab === 'open' && stagedEntries.length}
        <div class="flex items-center gap-2 px-1 text-xs text-muted">
          <span>{includedEntries.length} of {stagedEntries.length} in the next message</span>
          <Button variant="ghost" size="sm" class="ml-auto text-accent" disabled={including} onclick={() => includeOnly(includedEntries.length === stagedEntries.length ? [] : stagedEntries)}>{includedEntries.length === stagedEntries.length ? 'Clear' : 'Include all'}</Button>
        </div>
      {/if}
      {#if !loading && !shown.length}<p class="p-2 text-muted">{tab === 'open' ? 'No open comments. Select text or a diff line to add one.' : 'No resolved comments.'}</p>{/if}
      {#each shown as thread (thread.thread_id)}
        <article class="rounded-lg p-2" class:opacity-70={thread.state === 'resolved'} aria-label={`${thread.state === 'resolved' ? 'Resolved' : 'Open'} comment`}>
          <TargetQuote summary={targetSummary({ locator: thread.anchor.locator, source: thread.anchor.source, sourceTitle: null })} onLocate={() => locate(thread)} />
          {#if thread.entries[0]}
            {@const entry = thread.entries[0]}
            {@const staged = thread.state === 'open' && sendableEntry(entry) && stagedRows.has(entry.entry_id)}
            {#snippet badge()}<StatusBadge status={`comment_${commentStatus(thread)}`} />{/snippet}
            <div class="mt-2 flex items-start gap-2">
              {#if staged}<input type="checkbox" class="mt-1.5 h-4 w-4 shrink-0 cursor-pointer accent-[rgb(var(--color-accent))]" aria-label={`Include in message: ${entry.body}`} title="Include in the next message" disabled={including}
                checked={stagedRows.get(entry.entry_id)?.inclusion === 'included'} onchange={event => toggleIncluded(entry, event.currentTarget.checked)} />{/if}
              <Button variant="link" size="inline" class="flex min-w-0 flex-1 select-text items-start text-left text-foreground hover:no-underline" aria-label={`Locate comment: ${entry.body}`} onclick={() => locate(thread)}>
                <CommentEntry {entry} clamp aside={badge} />
              </Button>
            </div>
            <!-- Human replies staged for this chat are chosen on their own, like the comment above. -->
            {#each thread.state === 'open' ? thread.entries.slice(1).filter(reply => sendableEntry(reply) && stagedRows.has(reply.entry_id)) : [] as reply (reply.entry_version_id)}
              <label class="ml-6 mt-1 flex cursor-pointer items-start gap-2 text-xs">
                <input type="checkbox" class="mt-0.5 h-4 w-4 shrink-0 accent-[rgb(var(--color-accent))]" aria-label={`Include in message: ${reply.body}`} disabled={including}
                  checked={stagedRows.get(reply.entry_id)?.inclusion === 'included'} onchange={event => toggleIncluded(reply, event.currentTarget.checked)} />
                <span class="min-w-0 flex-1 text-secondary"><span class="font-medium text-foreground">Reply</span> · <span class="line-clamp-2">{reply.body}</span></span>
              </label>
            {/each}
          {/if}
          <CommentReplies {thread} onReply={body => reply(thread, body)} disabled={!!changing} />
          <div class="-ml-2 flex flex-wrap">
            <Button variant="ghost" size="sm" class="text-muted" disabled={!!changing} onclick={() => change(thread, 'resolve')}>{thread.state === 'open' ? 'Resolve' : 'Reopen'}</Button>
            {#if thread.entries[0]?.author.is_self}<Button variant="ghost" size="sm" class="text-muted" disabled={!!changing} onclick={() => change(thread, 'remove')}>Remove</Button>{/if}
          </div>
        </article>
      {/each}
      {/if}
    </div>
    {#if !handoff && includedEntries.length}
      <footer class="shrink-0 border-t border-separator px-3 py-1">
        <Button variant="ghost" size="sm" class="-ml-2 text-muted" disabled={including} onclick={() => { handoff = true; }}>Send to a new conversation instead…</Button>
      </footer>
    {/if}
  </section>
{:else if $workspaceState.active}
  {#key $workspaceState.active}
    <FeedbackInline controller={$workspaceState.active}>
      {#snippet comments()}<FeedbackPanel controller={$workspaceState.active!} />{/snippet}
    </FeedbackInline>
  {/key}
{/if}
