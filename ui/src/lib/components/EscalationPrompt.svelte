<script lang="ts" module>
  // Bounded so the note stays a short annotation, not a second message
  // composer. Matches the general "bounded textarea" contract for
  // approval/denial notes across the escalation flow.
  export const ESCALATION_NOTE_MAX_LENGTH = 500;
</script>

<script lang="ts">
  import Button from '$lib/components/ui/Button.svelte';
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

  // The note is scoped to the escalation currently shown. When the active
  // item changes (settled, expired, or the next queued escalation takes its
  // place) any unsent draft for the previous item must not leak forward.
  let lastItemCallId = $state('');
  $effect(() => {
    if (item.call_id !== lastItemCallId) {
      lastItemCallId = item.call_id;
      note = '';
      submitting = null;
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

<article class="rounded-3xl border border-sky-500/30 bg-sky-500/10 px-4 py-4 shadow-card">
  <div class="flex flex-wrap items-center justify-between gap-4">
    <div>
      <p class="text-xs font-medium uppercase tracking-[0.25em] text-sky-200">Approval required</p>
      <h3 class="mt-1 text-base font-semibold text-white">{item.tool_name ?? 'Escalated action'}</h3>
    </div>
    <div class="flex items-center gap-2">
      {#if queuedCount > 0}
        <span class="rounded-full border border-sky-300/40 px-2.5 py-0.5 text-xs font-medium text-sky-200">
          +{queuedCount} queued
        </span>
      {/if}
      <span class="rounded-full border border-sky-300/40 px-3 py-1 text-xs font-semibold uppercase tracking-[0.2em] text-sky-100">
        {expired ? 'Expired' : `${Math.max(secondsRemaining, 0)}s left`}
      </span>
    </div>
  </div>

  {#if item.session_id}
    <p class="mt-1 text-xs text-sky-300/60">Session: {item.session_id.slice(0, 12)}...</p>
  {/if}

  <ToolInvocationPreview
    toolName={item.tool_name}
    argumentsDisplay={item.arguments_display}
    reasoning={item.reasoning}
    risk={item.risk}
  />

  <label class="mt-3 block text-xs text-sky-200/80" for="escalation-note-{item.call_id}">
    Note (optional)
  </label>
  <textarea
    id="escalation-note-{item.call_id}"
    class="mt-1 w-full resize-none rounded-2xl border border-sky-400/25 bg-slate-950/40 px-3 py-2 text-sm text-sky-50 outline-none placeholder:text-sky-100/40 focus:border-sky-300/60"
    rows="2"
    maxlength={ESCALATION_NOTE_MAX_LENGTH}
    placeholder="Add context for this decision…"
    disabled={busy || expired}
    value={note}
    oninput={(event) => { note = event.currentTarget.value; }}
  ></textarea>

  <p class="mt-3 text-xs text-sky-200/70">
    You can also type <code class="rounded bg-sky-900/40 px-1 py-0.5">/approve</code> or <code class="rounded bg-sky-900/40 px-1 py-0.5">/deny</code> in the chat input.
  </p>

  <div class="mt-4 flex flex-wrap gap-2">
    <Button disabled={busy || expired} size="sm" onclick={() => void submit('approve')}>
      {submitting === 'approve' ? escalationSubmittingLabel('approve') : 'Approve'}
    </Button>
    <Button disabled={busy || expired} size="sm" variant="danger" onclick={() => void submit('deny')}>
      {submitting === 'deny' ? escalationSubmittingLabel('deny') : 'Deny'}
    </Button>
  </div>
</article>
