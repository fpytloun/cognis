<script lang="ts">
  import { onMount, untrack } from 'svelte';
  import { conversationFeedback, type ConversationFeedback } from '$lib/feedback/staging';
  import { FeedbackDeliveryModel } from '$lib/feedback/delivery';
  import { documentCoordination } from '$lib/document-coordination';
  import { getCommentWorkspace } from '$lib/feedback/comment-workspace';
  import type { FeedbackPreflight, IncludedFeedback, FeedbackStagingSnapshot } from '$lib/feedback/types';
  import { loadComposerPresentation } from './composer-presentation-loader';
  import Button from '$lib/components/ui/Button.svelte';

  /**
   * Comments travel with a message like attachments: chips inside the composer row. The chat's
   * comment list (the Comments panel) chooses them; without one, a small list opens above.
   */
  let { conversationId, included, onReadyChange }: {
    conversationId: string; included: IncludedFeedback | null; onReadyChange?: (ready: boolean) => void;
  } = $props();
  const workspace = getCommentWorkspace();
  const workspaceState = workspace?.state;
  let channel = $state.raw<ConversationFeedback | null>(null);
  let model = $state.raw<FeedbackDeliveryModel | null>(null);
  let snapshot = $state.raw<FeedbackStagingSnapshot | null>(null);
  let packet = $state.raw<FeedbackPreflight | null>(null);
  let presentation = $state.raw<Awaited<ReturnType<typeof loadComposerPresentation>> | null>(null);
  let listOpen = $state(false);
  let deliveryActive = $state(false);
  let loadAttempt = $state(0);
  let loadError = $state('');
  let error = $state('');
  let loading = $state(true);
  let busy = $state(false);
  let now = $state(Date.now());
  let sessionGeneration = $state(0);
  let recovered = '';
  let recoveries = 0;
  const scoped = $derived(channel?.conversationId === conversationId);
  const panelOpen = $derived(workspaceState ? $workspaceState!.list : listOpen);
  onMount(() => documentCoordination().onInvalidated(() => {
    channel = null; snapshot = null; packet = null; sessionGeneration++;
  }));
  $effect(() => {
    void sessionGeneration;
    const id = conversationId;
    channel = null; model = null; snapshot = null; packet = null; presentation = null;
    listOpen = false; deliveryActive = false; error = ''; loadError = ''; recovered = ''; recoveries = 0;
    let current: ConversationFeedback;
    let delivery: FeedbackDeliveryModel;
    try { current = conversationFeedback(id); delivery = new FeedbackDeliveryModel(id); }
    catch (caught) {
      error = caught instanceof Error ? caught.message : 'Feedback is unavailable until the account is verified.';
      return;
    }
    channel = current; model = delivery;
    const stops = [
      current.snapshot.subscribe(value => { snapshot = value; }),
      current.error.subscribe(value => { error = value; }),
      current.loading.subscribe(value => { loading = value; }),
      current.busy.subscribe(value => { busy = value; }),
      delivery.state.subscribe(value => { if (value.pending.length) deliveryActive = true; }),
      delivery.start(),
    ];
    const refresh = () => { void current.refresh(); };
    refresh();
    window.addEventListener('feedback-changed', refresh);
    window.addEventListener('focus', refresh);
    window.addEventListener('storage', refresh);
    return () => {
      for (const stop of stops) stop();
      window.removeEventListener('feedback-changed', refresh);
      window.removeEventListener('focus', refresh);
      window.removeEventListener('storage', refresh);
    };
  });
  $effect(() => {
    packet = null;
    if (!included || !channel || !scoped) return;
    try {
      const reviewed = untrack(() => channel!.review(included!));
      if (reviewed?.destination.kind === 'origin_chat' && reviewed.destination.conversation_id === conversationId) packet = reviewed;
    } catch { packet = null; }
  });
  // Delivery retries and the fallback list are lazy; the chips themselves are not.
  $effect(() => {
    void loadAttempt;
    const owner = channel;
    if (!owner || !scoped || !(deliveryActive || (listOpen && !workspace)) || presentation) return;
    const lease = documentCoordination().capture();
    let alive = true;
    loadError = '';
    void loadComposerPresentation().then(loaded => {
      if (!alive || channel !== owner || owner.conversationId !== conversationId) return;
      lease.assertCurrent();
      presentation = loaded;
    }).catch(caught => {
      if (alive && channel === owner && !lease.signal.aborted) loadError = caught instanceof Error ? caught.message : 'Feedback details could not be loaded.';
    });
    return () => { alive = false; };
  });
  const includedRows = $derived(snapshot?.items.filter(item => item.inclusion === 'included') ?? []);
  const ready = $derived((snapshot?.items.length ?? 0) - (included ? includedRows.length : 0));
  const count = $derived(packet?.selection.length ?? includedRows.length);
  const fresh = $derived(Boolean(packet && snapshot && Date.parse(packet.expires_at) > now
    && packet.selection.length === includedRows.length
    && packet.selection.every(selection => includedRows.some(item => item.entry_version_id === selection.entry_version_id
      && item.staging_version === selection.staging_version))));
  // A packet that expired, went missing or no longer matches staging (an edited or resolved
  // comment, another tab) is re-reviewed silently; the message waits until that finishes.
  $effect(() => {
    if (!included || !channel || !scoped || loading || busy || !snapshot || fresh) return;
    const key = included.creation_key;
    if (recovered === key) return;
    recovered = key;
    const owner = channel;
    // A re-review that still does not match twice in a row is a real failure, not a race.
    if (++recoveries > 2) { error = 'Included comments could not be updated.'; return; }
    void owner.reinclude().catch(caught => {
      if (channel === owner) error = caught instanceof Error ? caught.message : 'Included comments could not be updated.';
    });
  });
  $effect(() => { if (fresh) recoveries = 0; });
  const valid = $derived(!busy && (!included || Boolean(scoped && !loading && !error && fresh)));
  $effect(() => { onReadyChange?.(valid); });
  onMount(() => { const timer = setInterval(() => { now = Date.now(); }, 10000); return () => clearInterval(timer); });
  // Everything that was included is gone (sent, resolved, removed): close the fallback list.
  $effect(() => { if (listOpen && !workspace && snapshot && !loading && !snapshot.items.length) untrack(() => { listOpen = false; }); });
  function toggleList() {
    if (workspace) { if ($workspaceState!.list) workspace.closeComments(); else workspace.showComments(); return; }
    listOpen = !listOpen;
  }
  function run(change: (owner: ConversationFeedback) => Promise<void>) {
    const owner = channel;
    if (!owner || !scoped || busy) return;
    error = '';
    void change(owner).catch(caught => {
      if (channel === owner) error = caught instanceof Error ? caught.message : 'Comments could not be updated.';
    });
  }
</script>
{#snippet icon()}<svg class="h-3.5 w-3.5 shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><path d="M21 15a3 3 0 0 1-3 3H8l-5 4V6a3 3 0 0 1 3-3h12a3 3 0 0 1 3 3z" stroke-linejoin="round" /></svg>{/snippet}
{#snippet noun(value: number)}<span class="chip-noun">{value === 1 ? ' comment' : ' comments'}</span>{/snippet}
<!-- Chips sit in the composer row; an idle chat shows nothing. -->
<span class="feedback-tray inline-flex shrink-0 items-center gap-1" role="group" aria-label="Message feedback"
  hidden={!(channel && scoped && (included || ready)) && !error}>
  {#if channel && scoped}
    {#if included}
      <span class="chip border-accent/40 bg-accent-subtle text-accent" class:opacity-60={busy}>
        <Button variant="ghost" size="inline" aria-label={`Comments in message: ${count}`} aria-expanded={panelOpen} disabled={busy} onclick={toggleList}>{@render icon()}{count}{@render noun(count)}</Button>
        <Button variant="ghost" size="inline" aria-label="Remove comments from message" disabled={busy} onclick={() => run(owner => owner.exclude())}>×</Button>
      </span>
    {/if}
    {#if ready > 0}
      <span class="chip border-control-border text-secondary" class:opacity-60={busy}>
        <Button variant="ghost" size="inline" aria-label={`Add ${ready} ${ready === 1 ? 'comment' : 'comments'} to message`} disabled={busy} onclick={() => run(owner => owner.addAll())}>{#if !included}{@render icon()}{/if}+{ready}{#if !included}{@render noun(ready)}{/if}</Button>
        <Button variant="ghost" size="inline" aria-label="Choose comments" aria-expanded={panelOpen} disabled={busy} onclick={toggleList}>▾</Button>
      </span>
    {/if}
  {/if}
  {#if error}<Button variant="ghost" size="inline" class="chip border-danger-border px-2 text-danger" title={error} onclick={() => { error = ''; sessionGeneration++; }}>Comments failed · Retry</Button>{/if}
</span>
{#if channel && scoped && ((listOpen && !workspace) || deliveryActive || loadError)}
  <!-- Above the composer, so the row keeps its height. -->
  <div class="absolute inset-x-0 bottom-full z-10 mb-2 space-y-2 text-sm">
    {#if loadError}<p role="alert" class="text-danger">{loadError} <Button variant="link" size="inline" onclick={() => { loadAttempt++; }}>Retry</Button></p>{/if}
    {#if presentation && listOpen && !workspace}
      {#key channel}<presentation.Details channel={channel} {packet} onClose={() => { listOpen = false; }} />{/key}
    {/if}
    {#if presentation && model && deliveryActive}{#key model}<presentation.Delivery {model} />{/key}{/if}
  </div>
{/if}
<style>
  /* Attachment-like chips; touch targets keep 44px through an invisible hit area. */
  .chip { display: inline-flex; overflow: hidden; border-width: 1px; border-style: solid; border-radius: 9999px; font-size: .75rem; white-space: nowrap; }
  .chip :global(button) { position: relative; gap: .3rem; min-height: 1.75rem; padding: 0 .5rem; border-radius: 0; font-size: inherit; color: inherit; }
  .chip :global(button)::after { content: ''; position: absolute; inset: -8px 0; }
  .chip :global(button + button) { border-left: 1px solid rgb(var(--color-separator)); }
  .chip :global(button:hover) { background: rgb(var(--color-surface-hover)); color: inherit; }
  /* The global offset ring would fill the clipped chip segment; draw it inside instead. */
  .chip :global(button:focus-visible) { box-shadow: inset 0 0 0 2px rgb(var(--color-accent)); }
  /* Narrow composers keep the count only, so the text field keeps its room. */
  @container chat-composer (max-width: 480px) { .chip-noun { display: none; } }
</style>
