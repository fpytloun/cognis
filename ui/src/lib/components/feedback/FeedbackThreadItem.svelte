<script lang="ts">
  import { untrack } from 'svelte';
  import { presentationAuthority, type FeedbackPresentationPort } from '$lib/feedback/presentation';
  import type { FeedbackThread } from '$lib/feedback/types';
  import { libraryHref } from '$lib/library';
  import { feedbackSourceState, targetSummary } from '$lib/feedback/target-summary';
  import { commentStatus, sendableEntry } from '$lib/feedback/thread-status';
  import SavedFeedbackCapture from './SavedFeedbackCapture.svelte';
  import FeedbackPublicOutcome from './FeedbackPublicOutcome.svelte';
  import TargetQuote from './TargetQuote.svelte';
  import CommentEntry from './CommentEntry.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import StatusBadge from '$lib/components/ui/StatusBadge.svelte';
  /** One comment: what it targets, what was said, where it stands, and the few actions that matter. */
  let { thread, port, selected, onSelect, showTarget = true, selectable = false }: {
    thread: FeedbackThread; port: FeedbackPresentationPort; selected: string[]; onSelect: (id: string, selected: boolean) => void; showTarget?: boolean; selectable?: boolean;
  } = $props();
  let editId = $state<string | null>(null);
  let edited = $state('');
  let busy = $state(false);
  let removing = $state(false);
  let observation = $state(false);
  const snapshot = $derived(port.state);
  const session = $derived(port.session.state);
  // Lease revocation invalidates the session without replacing the snapshot.
  const authority = $derived.by(() => { $session; return presentationAuthority(port, $snapshot); });
  const binding = $derived(port.binding());
  const sourceState = $derived(feedbackSourceState(thread.anchor, $snapshot.source));
  const target = $derived(targetSummary({ locator: thread.anchor.locator, source: thread.anchor.source, sourceTitle: sourceState === 'current' ? $snapshot.source?.title ?? null : null }));
  const status = $derived(commentStatus(thread));
  const own = $derived(thread.entries[0]?.author.is_self ?? false);
  const canChange = $derived(!!authority && !!$snapshot.source?.capabilities.comment && ($snapshot.source.capabilities.moderate || own));
  const canRemove = $derived(!!port.moderate && !!authority && !thread.hidden && ($snapshot.source?.capabilities.moderate || own));
  // Private input never survives an account, source, context or grant change.
  $effect(() => { authority; untrack(() => { editId = null; edited = ''; removing = false; }); });
  // A saved new version closes the editor so the stored body is shown.
  $effect(() => { const bodies = thread.entries.map(entry => entry.entry_version_id).join(); untrack(() => { if (bodies && editId && !busy) {
    const entry = thread.entries.find(value => value.entry_id === editId);
    if (!entry || entry.entry_version_id !== editedVersion) editId = null;
  } }); });
  let editedVersion = '';
  function canEdit(entry: FeedbackThread['entries'][number]) { return port.canEdit ? port.canEdit(entry) : entry.author.is_self; }
  async function run(action: () => Promise<unknown> | unknown) {
    busy = true;
    try { await action(); } catch { /* The owner reports the error and keeps input. */ } finally { busy = false; }
  }
  function startEdit(entry: FeedbackThread['entries'][number]) { editId = entry.entry_id; edited = entry.body; editedVersion = entry.entry_version_id; }
  async function saveEdit(entry: FeedbackThread['entries'][number]) {
    await run(async () => { await port.edit(entry, edited); editId = null; });
  }
</script>
<article class="border-b border-separator py-2 last:border-b-0" class:opacity-70={thread.state === 'resolved'} aria-label={`${thread.state === 'resolved' ? 'Resolved' : 'Open'} comment`} data-comment-status={status}>
  {#if showTarget}<div class="mb-1"><TargetQuote summary={target} onLocate={() => void port.locate(thread)} /></div>{/if}
  {#each thread.entries as entry, index (entry.entry_version_id)}
    {#snippet badge()}{#if index === 0}<StatusBadge status={`comment_${status}`} />{/if}{/snippet}
    <div class="py-1">
      <div class="flex items-start gap-2">
        {#if selectable && thread.state === 'open' && sendableEntry(entry)}<input class="mt-1.5 h-4 w-4 shrink-0 cursor-pointer accent-[rgb(var(--color-accent))] disabled:cursor-not-allowed" aria-label={`Include in message: ${entry.body}`} title="Send this comment" type="checkbox" disabled={!$snapshot.source?.capabilities.dispatch} checked={selected.includes(entry.entry_version_id)} onchange={(event) => onSelect(entry.entry_version_id, event.currentTarget.checked)} />{/if}
        <div class="min-w-0 flex-1">
          {#if editId === entry.entry_id}
            <CommentEntry {entry} body={false} />
            <label class="mt-1 block"><span class="sr-only">Edit comment</span><textarea class="w-full rounded-md border border-separator bg-canvas p-2 text-sm text-foreground focus:border-accent focus:outline-none" bind:value={edited} maxlength="16384"></textarea></label>
            <div class="flex justify-end gap-1">
              <Button variant="ghost" size="sm" onclick={() => editId = null}>Cancel</Button>
              <Button size="sm" disabled={busy || !edited.trim() || edited === entry.body} onclick={() => saveEdit(entry)}>Save</Button>
            </div>
          {:else}
            <Button variant="link" size="inline" class="flex w-full select-text items-start text-left text-foreground hover:no-underline" aria-label={`Locate comment: ${entry.body}`} onclick={() => void port.locate(thread)}>
              <CommentEntry {entry} aside={index === 0 ? badge : undefined} />
            </Button>
          {/if}
          {#if entry.author.kind === 'guest_unverified' && $snapshot.source?.capabilities.moderate}
            <FeedbackPublicOutcome subjectId={$snapshot.source.subject_id} entryVersionId={entry.entry_version_id} />
          {/if}
        </div>
        {#if editId !== entry.entry_id && canEdit(entry)}<Button variant="ghost" size="sm" class="shrink-0 text-muted" aria-label={`Edit comment: ${entry.body}`} onclick={() => startEdit(entry)}>Edit</Button>{/if}
      </div>
    </div>
  {/each}
  <div class="-ml-2 flex flex-wrap items-center">
    {#if port.resolve && canChange}<Button variant="ghost" size="sm" class={status === 'answered' ? 'text-accent' : 'text-muted'} disabled={busy} onclick={() => run(() => port.resolve?.(thread))}>{thread.state === 'open' ? 'Resolve' : 'Reopen'}</Button>{/if}
    {#if canRemove}
      {#if removing}
        <Button variant="ghost" size="sm" class="text-danger" disabled={busy} onclick={() => run(() => port.moderate?.(thread, 'Removed'))}>Confirm remove</Button>
        <Button variant="ghost" size="sm" class="text-muted" onclick={() => removing = false}>Keep</Button>
      {:else}<Button variant="ghost" size="sm" class="text-muted" onclick={() => removing = true}>Remove</Button>{/if}
    {/if}
    {#if sourceState !== 'current' && thread.anchor.source.kind === 'document'}<a class="feedback-action text-accent" href={libraryHref({ kind: thread.anchor.source.content_ref.startsWith('dlv_') ? 'deliverable' : 'artifact', id: thread.anchor.source.content_ref })}>View original revision</a>{/if}
    {#if thread.anchor.locator.kind === 'html' && thread.anchor.locator.capture_id}
      <Button variant="ghost" size="sm" class="text-accent" onclick={() => observation = !observation}>{observation ? 'Hide' : 'Show'} capture</Button>
    {/if}
  </div>
  {#if observation && thread.anchor.locator.kind === 'html' && thread.anchor.locator.capture_id && port.capturePixels}<SavedFeedbackCapture captureId={thread.anchor.locator.capture_id} locator={thread.anchor.locator} threadId={thread.thread_id} {binding} loadPixels={port.capturePixels} />{/if}
</article>
