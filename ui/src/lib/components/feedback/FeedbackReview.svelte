<script lang="ts">
  import { onDestroy, onMount, untrack } from 'svelte';
  import type { FeedbackEntryVersion, FeedbackPreflight, FeedbackContext, SubmissionReceipt } from '$lib/feedback/types';
  import { feedbackApi, type FeedbackDestinationOption } from '$lib/feedback/api';
  import { conversationFeedback } from '$lib/feedback/staging';
  import type { FeedbackReviewSource } from '$lib/feedback/review-selection';
  import { documentCoordination } from '$lib/document-coordination';
  import { FeedbackDispatchJournal, type FeedbackDispatchRecord } from '$lib/feedback/dispatch-journal';
  import FeedbackSubmission from './FeedbackSubmission.svelte';
  import ConversationLink from '$lib/components/ConversationLink.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import type { FeedbackSurface } from '$lib/feedback/api';
  // The page decides the recommended destination: a task view revises, elsewhere starts fresh.
  // Read from the location so the standalone (public) bundle stays free of app-only modules.
  const path = typeof location === 'undefined' ? '' : location.pathname;
  const surface: FeedbackSurface = path.startsWith('/tasks') ? 'task' : path.startsWith('/library') ? 'library' : 'other';
  import { conversationChooser } from '$lib/feedback/conversation-chooser';
  // Signed-in only: the app provides the chat picker; public viewers never offer it.
  const chooser = conversationChooser();
  let chosenConversation = $state<{ conversation_id: string; title: string | null } | null>(null);
  const OTHER = 'other';
  // embedded: the host panel already shows the title and the way back, so skip the own header.
  // bar: the comment list above owns selection; render only destination, note and Send.
  let { entries, sources = [], subjectIds, context, onClose, initialSelection, handoff = false, embedded = false, bar = false, onSent }: { entries: FeedbackEntryVersion[]; sources?: FeedbackReviewSource[]; subjectIds: string[]; context: FeedbackContext; onClose: () => void; initialSelection?: string[]; handoff?: boolean; embedded?: boolean; bar?: boolean; onSent?: () => void } = $props();
  const sent = (entry: FeedbackEntryVersion) => entry.delivery.some(item => item.delivery === 'accepted');
  // Everything not yet sent is selected; already sent comments stay available but unchecked.
  let chosen = $state<string[]>(untrack(() => initialSelection ?? entries.filter(entry => !sent(entry)).map(entry => entry.entry_version_id)));
  let active = true;
  const ownerLease = documentCoordination().capture();
  const lease = {
    assertCurrent() { if (!active) throw new Error('Feedback review changed.'); ownerLease.assertCurrent(); },
    async validate() { this.assertCurrent(); await ownerLease.validate(); this.assertCurrent(); },
  };
  onDestroy(() => { active = false; });
  const journal = untrack(() => context.kind === 'standalone' || handoff ? new FeedbackDispatchJournal(subjectIds) : null);
  let options = $state<FeedbackDestinationOption[]>([]);
  let destinationIndex = $state('');
  let choosingConversation = $state(false);
  const destinationHint = $derived(destinationIndex === '' || destinationIndex === OTHER
    ? '' : options[Number(destinationIndex)]?.description ?? '');
  const destinationLabel = $derived(destinationIndex === OTHER ? chosenConversation?.title ?? 'the selected chat'
    : options[Number(destinationIndex)]?.label ?? 'the agent');
  let overall = $state('');
  let preflight = $state<FeedbackPreflight | null>(null);
  let receipt = $state<SubmissionReceipt | null>(null);
  let frozen = $state<FeedbackDispatchRecord | null>(null);
  let storageReady = $state(!journal);
  let busy = $state(false);
  let error = $state('');
  let confirmedTask = $state(false);
  let creationKey: string = crypto.randomUUID();
  let reviewGeneration = 0;
  let fingerprint = '';
  // A frozen send shows its own selection; preflights may omit context items, so fall back to
  // the caller's entries with exactly the frozen version IDs.
  const displayedEntries = $derived.by(() => {
    if (!frozen) return entries;
    const frozenEntries = frozen.preflight.context_items.filter(item => item.role === 'instruction').map(item => item.entry);
    const ids = new Set(frozen.preflight.selection.map(item => item.entry_version_id));
    return frozenEntries.length ? frozenEntries : entries.filter(entry => ids.has(entry.entry_version_id));
  });
  const selectedEntries = $derived(displayedEntries.filter(entry => frozen || bar || chosen.includes(entry.entry_version_id)));
  let noting = $state(false);
  let notice = $state<{ count: number; label: string; admission: SubmissionReceipt['admission'] } | null>(null);
  const chatInclusion = $derived(context.kind === 'chat' && !handoff);
  const accepted = $derived(Boolean(frozen && receipt?.delivery === 'accepted'
    && receipt.creation_key === frozen.creation_key && receipt.manifest_hash === frozen.preflight.manifest_hash));
  const plural = (count: number) => `${count} ${count === 1 ? 'comment' : 'comments'}`;
  onMount(() => {
    if (!journal) return;
    void journal.load().then(async record => {
      lease.assertCurrent();
      // An accepted send needs no recovery; reopening starts a fresh review.
      if (record?.receipt?.delivery === 'accepted' && record.receipt.creation_key === record.creation_key
        && record.receipt.manifest_hash === record.preflight.manifest_hash) {
        await journal.clearAccepted(record.creation_key);
        lease.assertCurrent();
        record = null;
      }
      frozen = record;
      if (record) {
        preflight = record.preflight; receipt = record.receipt;
        overall = record.overall_message ?? '';
        creationKey = record.creation_key;
      }
      storageReady = true;
    }).catch(caught => { if (active) error = caught instanceof Error ? caught.message : 'Saved feedback send could not be read.'; });
  });
  $effect(() => {
    const next = JSON.stringify([entries.map(entry => [entry.entry_version_id, entry.version, entry.thread_id]), sources]);
    if (fingerprint && fingerprint !== next && !untrack(() => frozen)) invalidate();
    fingerprint = next;
  });
  $effect(() => {
    if (context.kind === 'standalone' || handoff) {
      void feedbackApi.destinations(context, subjectIds, surface).then(value => {
        lease.assertCurrent();
        options = handoff ? value.options.filter(option => option.destination?.kind === 'new_followup') : value.options;
        if (handoff && options.length === 1 && options[0].available) destinationIndex = '0';
        const recommended = options.findIndex(option => option.recommended && option.available);
        if (!handoff && destinationIndex === '' && recommended >= 0) destinationIndex = String(recommended);
      })
        .catch(caught => { if (active) error = caught instanceof Error ? caught.message : 'Destinations could not be loaded.'; });
    }
  });
  // Any input change discards a prepared packet locally; the next send prepares a fresh one.
  function invalidate() { if (frozen) return; reviewGeneration++; preflight = null; confirmedTask = false; }
  const canSend = $derived(!busy && storageReady && !receipt && selectedEntries.length > 0
    && destinationIndex !== '' && (destinationIndex !== OTHER || !!chosenConversation)
    && (!preflight?.task_consequences || confirmedTask));
  /**
   * An accepted send needs no recovery: show a short notice and clear the retained request so the
   * form is ready for the next comments. Comment rows show Sent from the refreshed threads.
   */
  async function settleAccepted(result: SubmissionReceipt) {
    if (!frozen || !journal) return;
    const sent = { count: frozen.preflight.selection.length, label: destinationLabel, admission: result.admission };
    await journal.clearAccepted(frozen.creation_key);
    lease.assertCurrent();
    reviewGeneration++;
    frozen = null; receipt = null; preflight = null; creationKey = crypto.randomUUID();
    overall = ''; noting = false; confirmedTask = false;
    notice = sent;
    onSent?.();
    window.dispatchEvent(new Event('feedback-changed'));
  }
  async function preview(): Promise<FeedbackPreflight | null> {
    const generation = ++reviewGeneration;
    await lease.validate();
    const destination = destinationIndex === OTHER
      ? chosenConversation && { kind: 'existing_followup' as const, conversation_id: chosenConversation.conversation_id }
      : options[Number(destinationIndex)]?.destination;
    if (destinationIndex === '' || !destination) throw new Error('Choose a destination.');
    const result = await feedbackApi.preflight({ context,
      selection: selectedEntries.map(entry => ({ entry_version_id: entry.entry_version_id, expected_entry_version: entry.version })),
      reviewed_guest_entry_version_ids: [], overall_message: overall || null, destination, update_mode: 'follow_request' });
    lease.assertCurrent();
    if (generation !== reviewGeneration) return null;
    preflight = result;
    return result;
  }
  async function includeSelected() {
    if (!chatInclusion || context.kind !== 'chat' || busy || !selectedEntries.length) return;
    const generation = ++reviewGeneration;
    busy = true; error = '';
    try {
      await lease.validate();
      if (generation !== reviewGeneration) return;
      // Adds to what the message already includes, e.g. comments on other documents in this chat.
      await conversationFeedback(context.conversation_id).add($state.snapshot(selectedEntries), $state.snapshot(sources), lease);
      lease.assertCurrent(); onClose();
    } catch (caught) { if (active) error = caught instanceof Error ? caught.message : 'Comments could not be included.'; }
    finally { if (active) busy = false; }
  }
  /** One click: prepare the packet, stop only for task consequences, then send it. */
  async function send() {
    if (!canSend && !frozen) return;
    busy = true; error = '';
    try {
      let packet = frozen ? frozen.preflight : preflight;
      if (!packet) {
        packet = await preview();
        if (!packet || packet.task_consequences) return;
      }
      if (!journal) throw new Error('Durable feedback dispatch is unavailable.');
      // Domain persistence receives a plain reviewed DTO, never Svelte's deep proxy.
      const reviewed = $state.snapshot(packet);
      await lease.validate();
      frozen = await journal.freeze(reviewed, creationKey, overall || null);
      // A reload or response loss replays this same immutable request only.
      let result: SubmissionReceipt;
      try { result = await feedbackApi.receipt(frozen.creation_key); }
      catch (caught) {
        if (!(caught && typeof caught === 'object' && 'status' in caught && caught.status === 404)) throw caught;
        await lease.validate();
        result = await feedbackApi.submit({ creation_key: frozen.creation_key,
          preflight_id: frozen.preflight.preflight_id, manifest_hash: frozen.preflight.manifest_hash,
          replaces_rejected_submission_id: null });
      }
      lease.assertCurrent();
      receipt = result;
      await journal.record(result);
      if (result.delivery === 'accepted') await settleAccepted(result);
    } catch (caught) { if (active) error = caught instanceof Error ? caught.message : 'Delivery is unknown. Check delivery before trying again.'; }
    finally { busy = false; }
  }
  async function checkSaved() {
    if (!frozen || !journal) return;
    busy = true;
    try {
      let result = await feedbackApi.receipt(frozen.creation_key);
      await lease.validate();
      if (result.check_delivery_available) result = await feedbackApi.check(result.submission_id);
      await journal.record(result);
      lease.assertCurrent(); receipt = result;
      if (accepted) await settleAccepted(result);
    } catch (caught) { if (active) error = caught instanceof Error ? caught.message : 'Delivery is unknown. The original request is retained.'; }
    finally { busy = false; }
  }
</script>
<section aria-label="Review comments" class="text-sm">
  {#if !embedded}<div class="flex items-center justify-between"><h3 class="font-semibold">{handoff ? 'Start new conversation' : 'Send comments'}</h3><Button variant="ghost" size="icon" class="text-lg text-muted" disabled={busy} aria-label="Close review" onclick={onClose}>×</Button></div>{/if}
  {#if notice}
    <div class="mb-2 flex flex-wrap items-center gap-x-2 rounded-lg bg-success-subtle px-3 py-2" role="status">
      <p class="min-w-0 flex-1 text-sm">Sent {plural(notice.count)} to {notice.label}.</p>
      {#if notice.admission?.kind === 'conversation_turn'}<ConversationLink class="text-sm font-medium text-accent hover:underline" conversationId={notice.admission.conversation_id}>Open conversation</ConversationLink>{/if}
      {#if notice.admission?.kind === 'task_attempt'}<a class="text-sm font-medium text-accent hover:underline" href={`/tasks/${encodeURIComponent(notice.admission.task_id)}`}>Open task</a>{/if}
      <Button variant="ghost" size="icon" class="-mr-2 text-muted" aria-label={handoff ? 'Done' : 'Dismiss'} onclick={() => { notice = null; if (handoff) onClose(); }}>×</Button>
    </div>
  {/if}
  {#if handoff && notice}<!-- The started conversation owns the rest. -->
  {:else}
  {#if !bar && !chatInclusion && !frozen && displayedEntries.length > 1}
    <div class="flex items-center gap-2 border-b border-separator">
      <span class="text-xs text-muted">{selectedEntries.length} of {displayedEntries.length} selected</span>
      <Button variant="ghost" size="sm" class="ml-auto text-accent" disabled={busy} onclick={() => { chosen = selectedEntries.length === displayedEntries.length ? [] : displayedEntries.map(entry => entry.entry_version_id); invalidate(); }}>{selectedEntries.length === displayedEntries.length ? 'Clear' : 'Select all'}</Button>
    </div>
  {:else if !bar && chatInclusion && displayedEntries.length > 1}
    <div class="flex gap-2 border-b border-separator">
      <Button variant="ghost" size="sm" class="text-accent" disabled={busy} onclick={() => { chosen = entries.map(entry => entry.entry_version_id); invalidate(); }}>Include all</Button>
      <Button variant="ghost" size="sm" class="text-muted" disabled={busy} onclick={() => { chosen = []; invalidate(); }}>Exclude all</Button>
    </div>
  {/if}
  {#each bar ? [] : displayedEntries as entry (entry.entry_version_id)}
    <div class="border-b border-separator py-2"><label class="flex cursor-pointer items-start gap-2"><input type="checkbox" class="mt-1 h-4 w-4 shrink-0 accent-[rgb(var(--color-accent))]" aria-label={`Include in message: ${entry.body}`} disabled={busy || !!frozen} checked={!!frozen || chosen.includes(entry.entry_version_id)}
      onchange={event => { chosen = event.currentTarget.checked ? [...chosen, entry.entry_version_id] : chosen.filter(id => id !== entry.entry_version_id); invalidate(); }} /><span class="min-w-0 flex-1"><span class="block whitespace-pre-wrap break-words">{entry.body}</span><span class="mt-1 block text-xs text-muted">{entry.author.label}{entry.author.kind === 'guest_unverified' ? ' · Guest' : ''}{entry.version > 1 ? ' · Edited' : ''}{sent(entry) && !frozen ? ' · Already sent' : ''}</span></span></label></div>
  {:else}
    {#if !bar}<p class="py-3 text-muted">No open comments to send.</p>{/if}
  {/each}
  {#if !chatInclusion}
    <!-- min-w-0: a fieldset otherwise grows to its widest unwrapped line and overflows. -->
    <fieldset class="min-w-0 text-sm" class:mt-3={!bar} aria-label="Send to" disabled={busy || !!frozen}>
      <label class="text-xs font-medium text-muted" for={`send-to-${subjectIds.join('-')}`}>Send to</label>
      <select id={`send-to-${subjectIds.join('-')}`} aria-label="Send to" class="mt-1 min-h-10 w-full min-w-0 rounded-lg border border-separator bg-canvas px-2 text-sm text-foreground"
        value={destinationIndex} onchange={(event) => {
          if (event.currentTarget.value === 'choose-chat') {
            choosingConversation = true;
            event.currentTarget.value = destinationIndex;
          } else {
            destinationIndex = event.currentTarget.value;
            choosingConversation = false;
            invalidate();
          }
        }}>
        <option value="" disabled>Choose a destination</option>
        {#each options as option, index}
          <option value={String(index)} disabled={!option.available || !option.destination}>
            {option.label}{option.reason ? ` — ${option.reason}` : ''}
          </option>
        {/each}
        {#if chosenConversation}<option value={OTHER}>Chat: {chosenConversation.title ?? 'Untitled conversation'}</option>{/if}
        {#if !handoff && chooser}<option value="choose-chat">Another chat…</option>{/if}
      </select>
      {#if destinationHint}<p class="mt-1 text-xs text-muted">{destinationHint}</p>{/if}
      {#if choosingConversation && chooser}
        {#await chooser() then { default: ConversationChooser }}
        <ConversationChooser selected={chosenConversation?.conversation_id ?? null}
          onSelect={conversation => { chosenConversation = { conversation_id: conversation.conversation_id, title: conversation.title ?? null }; destinationIndex = OTHER; choosingConversation = false; invalidate(); }} />
        {/await}
      {/if}
      {#if noting || overall || !bar}
        <label class="mt-2 block"><span class="text-xs font-medium text-muted">Note for the agent</span>
          <textarea class="mt-1 block w-full min-w-0 rounded-lg border border-separator bg-canvas px-2.5 py-2 focus:border-accent focus:outline-none" rows="2" aria-label="Instruction"
            placeholder="Optional" bind:value={overall} oninput={invalidate}></textarea></label>
      {/if}
    </fieldset>
  {/if}
  {#if chatInclusion}
    <Button size="sm" class={bar ? 'w-full' : 'mt-3'} disabled={busy || !selectedEntries.length} onclick={includeSelected}>{busy ? 'Including…' : selectedEntries.length ? `Include ${plural(selectedEntries.length)} in message` : 'Select comments to include'}</Button>
  {:else}
    {#if preflight?.task_consequences && !frozen}
      <p class="mt-2 text-sm text-warning">{preflight.task_consequences.side_effect_warning}</p>
      <p class="text-sm">Reopens {preflight.task_consequences.target_step}; invalidates {preflight.task_consequences.downstream_steps.join(', ') || 'no downstream steps'}.</p>
      <label class="flex min-h-11 items-center gap-2 text-sm"><input type="checkbox" checked={confirmedTask} onchange={(event) => { confirmedTask = event.currentTarget.checked; }} />I confirm these task consequences</label>
    {/if}
    {#if frozen}
      {#if receipt}<FeedbackSubmission {receipt} />{:else}<p class="mt-2 text-xs text-muted">The previous send did not confirm. Retrying sends the same request once.</p>{/if}
      <div class="mt-2 flex flex-wrap items-center gap-2">
        {#if !receipt || receipt.delivery === 'pending'}<Button size="sm" disabled={busy || !storageReady} onclick={send}>{busy ? 'Sending…' : 'Retry original request'}</Button>{/if}
        <Button variant="ghost" size="sm" class="text-accent" disabled={busy} onclick={checkSaved}>Check saved send</Button>
      </div>
    {:else}
      <div class="mt-2 flex items-center gap-2">
        {#if bar && !noting && !overall}<Button variant="ghost" size="sm" class="-ml-2 text-muted" disabled={busy} onclick={() => { noting = true; }}>+ Note</Button>{/if}
        <Button size="sm" class={bar ? 'ml-auto' : ''} disabled={!canSend} onclick={send}>{busy ? 'Sending…' : handoff ? 'Start conversation' : selectedEntries.length ? `Send ${plural(selectedEntries.length)}` : 'Select comments to send'}</Button>
      </div>
    {/if}
  {/if}
  {/if}
  {#if error}<p role="alert" class="mt-2 text-sm text-danger">{error}</p>{/if}
</section>
