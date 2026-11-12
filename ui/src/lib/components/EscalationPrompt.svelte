<script lang="ts" module>
  // Bounded so the note stays a short annotation, not a second message
  // composer. Matches the general "bounded textarea" contract for
  // approval/denial notes across the escalation flow.
  export const ESCALATION_NOTE_MAX_LENGTH = 500;
</script>

<script lang="ts">
  import Button from '$lib/components/ui/Button.svelte';
  import PendingRequestPanel from '$lib/components/PendingRequestPanel.svelte';
  import ToolInvocationPreview from '$lib/components/ToolInvocationPreview.svelte';
  import { escalationSubmittingLabel } from '$lib/chat-page';
  import type { Escalation } from '$lib/types/api';

  let {
    item,
    secondsRemaining,
    onApprove,
    onDeny,
    pending = false,
    queuedCount = 0,
  } = $props<{
    item: Escalation;
    secondsRemaining: number;
    onApprove: (note?: string) => void | Promise<void> | Promise<boolean>;
    onDeny: (note?: string) => void | Promise<void> | Promise<boolean>;
    pending?: boolean;
    queuedCount?: number;
  }>();

  let note = $state('');
  let submitting = $state<'approve' | 'deny' | null>(null);
  let collapsed = $state(false);

  // The note is scoped to the escalation currently shown. When the active
  // item changes (settled, expired, or the next queued escalation takes its
  // place) any unsent draft for the previous item must not leak forward.
  let lastItemCallId = $state('');
  $effect(() => {
    if (item.call_id !== lastItemCallId) {
      lastItemCallId = item.call_id;
      note = '';
      submitting = null;
      collapsed = false;
    }
  });

  const expired = $derived(secondsRemaining <= 0);
  const busy = $derived(pending || submitting !== null);

  async function submit(decision: 'approve' | 'deny'): Promise<void> {
    if (busy || expired) return;
    submitting = decision;
    const trimmedNote = note.trim() || undefined;
    try {
      const handler = decision === 'approve' ? onApprove : onDeny;
      const result = await handler(trimmedNote);
      // A handler that does not report success/failure (void) is treated as
      // successful for note-clearing purposes; handlers that report `false`
      // keep the note so the user does not have to retype it after a failed
      // request.
      if (result !== false) {
        note = '';
      }
    } finally {
      submitting = null;
    }
  }
</script>

<PendingRequestPanel id={`approval-details-${item.call_id}`} title={`Approval required · ${item.tool_name ?? 'Escalated action'}`} bind:collapsed>
  {#snippet status()}
      {#if queuedCount > 0}
        <span class="rounded-full border border-info-border px-2.5 py-0.5 text-xs font-medium text-info">
          +{queuedCount} queued
        </span>
      {/if}
      <span class="rounded-full border border-info-border px-3 py-1 text-xs font-semibold uppercase tracking-[0.2em] text-info">
        {expired ? 'Expired' : `${Math.max(secondsRemaining, 0)}s left`}
      </span>
  {/snippet}

  {#if item.session_id}
    <p class="mt-1 text-xs text-muted">Session: {item.session_id.slice(0, 12)}...</p>
  {/if}

  <ToolInvocationPreview
    toolName={item.tool_name}
    argumentsDisplay={item.arguments_display}
    reasoning={item.reasoning}
    risk={item.risk}
  />
  <p class="mt-2 break-all text-xs text-secondary">
    Approval ID: <code class="select-all">{item.call_id}</code>
  </p>

  <label class="mt-3 block text-xs text-secondary" for="escalation-note-{item.call_id}">
    Note (optional)
  </label>
  <textarea
    id="escalation-note-{item.call_id}"
    class="request-prose mt-1 w-full resize-none rounded-2xl border border-control-border bg-surface px-3 py-2 text-foreground outline-none placeholder:text-muted focus:border-accent"
    rows="2"
    maxlength={ESCALATION_NOTE_MAX_LENGTH}
    placeholder="Add context for this decision…"
    disabled={busy || expired}
    value={note}
    oninput={(event) => { note = event.currentTarget.value; }}
  ></textarea>

  <p class="mt-3 text-xs text-secondary">
    Resolve this action in chat with
    <code class="block select-all break-all rounded bg-inset px-1 py-0.5 mt-1">/approve --id {item.call_id}</code>
    or <code class="block select-all break-all rounded bg-inset px-1 py-0.5 mt-1">/deny --id {item.call_id}</code>.
    Add an optional note after the ID. Bare commands only work when one approval is pending.
  </p>

  {#snippet footer()}
  <div class="flex flex-wrap gap-2">
    <Button disabled={busy || expired} size="sm" onclick={() => void submit('approve')}>
      {submitting === 'approve' ? escalationSubmittingLabel('approve') : 'Approve'}
    </Button>
    <Button disabled={busy || expired} size="sm" variant="danger" onclick={() => void submit('deny')}>
      {submitting === 'deny' ? escalationSubmittingLabel('deny') : 'Deny'}
    </Button>
  </div>
  {/snippet}
</PendingRequestPanel>
