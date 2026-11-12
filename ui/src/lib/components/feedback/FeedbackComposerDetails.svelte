<script lang="ts">
  import { onMount, untrack } from 'svelte';
  import { documentCoordination } from '$lib/document-coordination';
  import type { ConversationFeedback } from '$lib/feedback/staging';
  import type { FeedbackEntryVersion, FeedbackPreflight, FeedbackThread } from '$lib/feedback/types';
  import type { FeedbackReviewSource } from '$lib/feedback/review-selection';
  import { targetSummary } from '$lib/feedback/target-summary';
  import FeedbackChromeMount from './FeedbackChromeMount.svelte';
  import { reviewChrome } from './review-chrome';
  import TargetQuote from './TargetQuote.svelte';
  import CommentEntry from './CommentEntry.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import Card from '$lib/components/ui/Card.svelte';
  import { revealComment } from '$lib/feedback/locate-target';
  /**
   * Choose the comments for the next message where the chat has no Comments panel (compact and
   * native chat windows). The main chat uses its Comments panel instead.
   */
  let { channel, packet, onClose }: { channel: ConversationFeedback; packet: FeedbackPreflight | null; onClose: () => void } = $props();
  const lease = untrack(() => documentCoordination().capture());
  const busyStore = untrack(() => channel.busy);
  let handoff = $state(false), loading = $state(true), error = $state('');
  let entries = $state<FeedbackEntryVersion[]>([]), threads = $state<FeedbackThread[]>([]), sources = $state<FeedbackReviewSource[]>([]);
  let alive = true;
  const selected = $derived(new Set(packet?.selection.map(item => item.entry_version_id) ?? []));
  onMount(() => { void load(); return () => { alive = false; }; });
  // The list follows the staging snapshot, so a change from the chip or another tab shows here.
  $effect(() => { void packet; untrack(() => { if (!loading) void load(); }); });
  async function load() {
    loading = true; error = '';
    try {
      const value = await channel.entries(lease.signal);
      if (!alive) return;
      entries = value.entries; threads = value.threads; sources = value.sources;
    } catch (caught) {
      if (alive && !lease.signal.aborted) error = caught instanceof Error ? caught.message : 'Could not load comments.';
    } finally { if (alive) loading = false; }
  }
  async function choose(ids: Set<string>) {
    error = '';
    try { await channel.setIncluded(entries.filter(entry => ids.has(entry.entry_version_id)), sources, lease); }
    catch (caught) { if (alive && !lease.signal.aborted) error = caught instanceof Error ? caught.message : 'The selected comments were not included. Try again.'; }
  }
  function toggle(id: string, on: boolean) {
    const next = new Set(selected);
    if (on) next.add(id); else next.delete(id);
    void choose(next);
  }
</script>
<Card class="max-h-[50dvh] overflow-y-auto rounded-xl bg-elevated p-3 shadow-lg">
<section aria-label="Comments in this message">
  {#if handoff}
    <FeedbackChromeMount definition={reviewChrome} viewProps={{
      entries: entries.filter(entry => selected.has(entry.entry_version_id)), sources,
      subjectIds: [...new Set(threads.map(thread => thread.subject_id))],
      context: { kind: 'chat', conversation_id: channel.conversationId }, handoff: true, initialSelection: [...selected],
      onClose: () => { handoff = false; },
    }} onClose={() => { handoff = false; }} />
  {:else}
    <header class="flex items-center gap-2 border-b border-separator pb-1">
      <h3 class="font-semibold">Comments in this message</h3>
      <span class="text-xs text-muted">{selected.size} of {entries.length}</span>
      <Button variant="ghost" size="sm" class="ml-auto text-accent" disabled={$busyStore || !entries.length} onclick={() => choose(new Set(selected.size === entries.length ? [] : entries.map(entry => entry.entry_version_id)))}>{selected.size === entries.length ? 'Clear' : 'Select all'}</Button>
      <Button variant="ghost" size="icon" class="-mr-2 text-muted" aria-label="Close" onclick={onClose}>×</Button>
    </header>
    {#each entries as entry (entry.entry_version_id)}
      {@const thread = threads.find(value => value.thread_id === entry.thread_id)}
      <div class="border-b border-separator py-2 last:border-b-0">
        {#if thread}<div class="mb-1 ml-6"><TargetQuote summary={targetSummary({ locator: thread.anchor.locator, source: thread.anchor.source, sourceTitle: null })} onLocate={() => void revealComment({ source: thread.anchor.source, locator: thread.anchor.locator })} /></div>{/if}
        <label class="flex cursor-pointer items-start gap-2"><input type="checkbox" class="mt-1.5 h-4 w-4 shrink-0 accent-[rgb(var(--color-accent))]" aria-label={`Include in message: ${entry.body}`} disabled={$busyStore} checked={selected.has(entry.entry_version_id)}
          onchange={event => toggle(entry.entry_version_id, event.currentTarget.checked)} />
          <CommentEntry {entry} clamp />
        </label>
      </div>
    {/each}
    {#if loading && !entries.length}<p role="status" class="py-2 text-xs text-muted">Loading comments…</p>{/if}
    {#if !loading && !entries.length}<p class="py-3 text-muted">No comments waiting. New comments on documents from this chat show up here.</p>{/if}
    {#if entries.length}<Button variant="ghost" size="sm" class="mt-1 text-muted" disabled={$busyStore || !selected.size} onclick={() => { handoff = true; }}>Send to a new conversation instead…</Button>{/if}
  {/if}
  {#if error}<p role="alert" class="py-2 text-danger">{error}</p>{/if}
</section>
</Card>
