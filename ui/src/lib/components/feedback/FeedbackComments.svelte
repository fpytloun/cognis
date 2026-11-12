<script lang="ts">
  import type { Snippet } from 'svelte';
  import type { FeedbackPresentationPort } from '$lib/feedback/presentation';
  import type { FeedbackEntryVersion, FeedbackThread } from '$lib/feedback/types';
  import type { FeedbackReviewSource } from '$lib/feedback/review-selection';
  import { feedbackSourceState, targetSummary } from '$lib/feedback/target-summary';
  import { commentStatus, entrySent, sendableEntry } from '$lib/feedback/thread-status';
  import FeedbackThreadItem from './FeedbackThreadItem.svelte';
  import TargetQuote from './TargetQuote.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  /**
   * The one comment list: rows with their status, and, for the owner, a send bar at the bottom
   * (`review` renders it for the selected comments).
   */
  let { port, guest, review, access, history }: {
    port: FeedbackPresentationPort; guest?: Snippet; review?: Snippet<[FeedbackEntryVersion[], FeedbackReviewSource[]]>; access?: Snippet; history?: Snippet;
  } = $props();
  const snapshot = $derived(port.state);
  const session = $derived(port.session.state);
  let resolved = $state(false);
  let resolving = $state(false);
  const view = $derived($session.surface ?? ($snapshot.view === 'threads' ? 'comments' : $snapshot.view));
  const counts = $derived({
    open: $snapshot.threads.filter(thread => thread.state !== 'resolved').length,
    resolved: $snapshot.threads.filter(thread => thread.state === 'resolved').length,
  });
  // Threads on the same exact anchor share one target heading instead of repeating its quote.
  const groups = $derived.by(() => {
    const result: { key: string; threads: FeedbackThread[] }[] = [];
    for (const thread of $snapshot.threads.filter(thread => (thread.state === 'resolved') === resolved)) {
      const key = JSON.stringify([thread.anchor.source, thread.anchor.locator, thread.anchor.content_digest]);
      const group = result.find(value => value.key === key);
      if (group) group.threads.push(thread); else result.push({ key, threads: [thread] });
    }
    return result;
  });
  // Only open human comments can be sent; agent answers and resolved threads stay out.
  const sendable = $derived($snapshot.threads.filter(thread => thread.state === 'open' && !thread.hidden)
    .map(thread => ({ thread, entries: thread.entries.filter(sendableEntry) }))
    .filter(value => value.entries.length));
  // Unsent comments start selected; an explicit tick or untick overrides that per comment version.
  let overrides = $state<Record<string, boolean>>({});
  const isSelected = (entry: FeedbackEntryVersion) => overrides[entry.entry_version_id] ?? !entrySent(entry);
  const chosen = $derived(sendable.map(({ thread, entries }) => ({ thread, entries: entries.filter(isSelected) }))
    .filter(value => value.entries.length));
  const selection = $derived(chosen.flatMap(value => value.entries.map(entry => entry.entry_version_id)));
  const answered = $derived($snapshot.threads.filter(thread => commentStatus(thread) === 'answered'));
  const canDispatch = $derived(!!review && !!$snapshot.source?.capabilities.dispatch);
  const canWhole = $derived(!!port.commentWhole && !!$snapshot.source?.capabilities.comment
    && !!$snapshot.source.permitted_locator_kinds.includes('whole') && $snapshot.source.source.kind !== 'transcript');
  function summary(thread: FeedbackThread) {
    const state = feedbackSourceState(thread.anchor, $snapshot.source);
    return targetSummary({ locator: thread.anchor.locator, source: thread.anchor.source, sourceTitle: state === 'current' ? $snapshot.source?.title ?? null : null });
  }
  function select(id: string, checked: boolean) { overrides = { ...overrides, [id]: checked }; }
  async function resolveAnswered() {
    if (!port.resolve || resolving) return;
    resolving = true;
    try { for (const thread of answered) await port.resolve(thread); }
    catch { /* The owner reports the error. */ }
    finally { resolving = false; }
  }
</script>
<section class="flex h-full max-h-[inherit] min-h-0 flex-col text-foreground" aria-label="Feedback panel">
  <header class="flex shrink-0 items-start justify-between gap-2" data-comments-title>
    <div class="min-w-0 flex-1">
      <h2 class="font-semibold">{view === 'thread' ? 'Comment' : 'Comments'}</h2>
      {#if $snapshot.source?.title && $snapshot.source.source.kind !== 'transcript'}<p class="truncate text-xs text-muted">{$snapshot.source.title}</p>{/if}
    </div>
    <Button variant="ghost" size="icon" class="text-muted" aria-label="Close comments" onclick={port.closeRail}>×</Button>
  </header>
  {#if $snapshot.error && !$session.composer}<p role="alert" class="py-2 text-sm text-danger">{$snapshot.error}</p>{#if !$snapshot.source}<Button variant="ghost" size="sm" class="text-accent" onclick={port.reload}>Retry loading comments</Button>{/if}{/if}
  <div class="mt-2 min-h-0 flex-1 overflow-auto">
    {@render guest?.()}
    {#if $snapshot.loading}<p role="status" class="py-3 text-sm text-muted">Loading comments…</p>{/if}
    {#if view === 'access' || view === 'history'}
      <Button variant="ghost" size="sm" class="-ml-2 text-accent" onclick={() => port.openRail('threads')}>Back to comments</Button>
      {#if view === 'access'}{@render access?.()}{:else}{@render history?.()}{/if}
    {:else if view === 'thread'}
      <Button variant="ghost" size="sm" class="-ml-2 text-accent" onclick={() => port.openRail('threads')}>All comments ({$snapshot.threads.length})</Button>
      {#each $snapshot.threads.filter(thread => thread.thread_id === $snapshot.selected) as thread (thread.thread_id)}
        <FeedbackThreadItem {thread} {port} selected={selection} onSelect={select} selectable={canDispatch} />
      {:else}<p role="status" class="py-3 text-sm">This thread is no longer available.</p>{/each}
    {:else}
      <div class="flex flex-wrap items-center gap-2">
        <div class="inline-flex rounded-md bg-canvas p-0.5 text-xs" role="group" aria-label="Show comments">
          <button type="button" class="rounded px-2 py-1 transition-colors" class:bg-surface-hover={!resolved} class:text-muted={resolved} aria-pressed={!resolved} onclick={() => resolved = false}>Open {counts.open}</button>
          <button type="button" class="rounded px-2 py-1 transition-colors" class:bg-surface-hover={resolved} class:text-muted={!resolved} aria-pressed={resolved} onclick={() => resolved = true}>Resolved {counts.resolved}</button>
        </div>
        {#if !resolved && answered.length && port.resolve}<Button variant="ghost" size="sm" class="text-accent" disabled={resolving} onclick={resolveAnswered}>Resolve {answered.length} answered</Button>{/if}
        {#if canWhole}<Button variant="ghost" size="sm" class="ml-auto text-accent" onclick={() => port.commentWhole?.()}>Comment on document</Button>{/if}
      </div>
      {#each groups as group (group.key)}
        <div class="mt-3">
          <TargetQuote summary={summary(group.threads[0])} onLocate={() => void port.locate(group.threads[0])} />
          {#each group.threads as thread (thread.thread_id)}
            <FeedbackThreadItem {thread} {port} selected={selection} onSelect={select} showTarget={false} selectable={canDispatch} />
          {/each}
        </div>
      {:else}<p class="py-6 text-center text-sm text-muted">{resolved ? 'No resolved comments.' : 'No open comments. Select text in the document to add one.'}</p>{/each}
      {#if $snapshot.next_cursor}<Button variant="ghost" size="sm" class="mt-2 text-accent" onclick={port.more}>Load more comments</Button>{/if}
    {/if}
  </div>
  {#if canDispatch && sendable.length && !resolved && (view === 'comments' || view === 'thread')}
    <footer class="mt-2 shrink-0 border-t border-separator pt-2" aria-label="Send comments">
      {@render review?.(
        chosen.flatMap(value => value.entries),
        chosen.flatMap(({ thread, entries }) => entries.map(entry => ({
          entry_version_id: entry.entry_version_id, version: entry.version, thread_id: thread.thread_id,
          anchor_id: thread.anchor.anchor_id, source: thread.anchor.source,
        }))))}
    </footer>
  {/if}
</section>
