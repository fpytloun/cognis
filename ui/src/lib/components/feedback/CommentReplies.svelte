<script lang="ts">
  import type { FeedbackThread } from '$lib/feedback/types';
  import { formatAbsoluteTime, formatRelativeTime } from '$lib/time';
  import AgentAvatar from '$lib/components/AgentAvatar.svelte';

  /**
   * Replies under a comment, kept quiet: the latest reply is shown, older ones fold behind a
   * count, and "Reply" opens one small input. Agent replies are marked so answers are easy to
   * spot.
   */
  let { thread, onReply, disabled = false }: {
    thread: FeedbackThread;
    onReply?: (body: string) => Promise<void>;
    disabled?: boolean;
  } = $props();
  const replies = $derived(thread.entries.slice(1));
  let expanded = $state(false), writing = $state(false), body = $state(''), busy = $state(false), error = $state('');
  const visible = $derived(expanded ? replies : replies.slice(-1));
  async function send() {
    if (!onReply || !body.trim() || busy) return;
    busy = true; error = '';
    try { await onReply(body.trim()); body = ''; writing = false; expanded = true; }
    catch (caught) { error = caught instanceof Error ? caught.message : 'The reply could not be sent.'; }
    finally { busy = false; }
  }
</script>

{#if replies.length}
  <div class="mt-2 space-y-2" aria-label="Replies">
    {#if replies.length > 1 && !expanded}
      <button type="button" class="text-xs text-muted hover:text-foreground" onclick={() => expanded = true}>
        {replies.length - 1} earlier {replies.length === 2 ? 'reply' : 'replies'}
      </button>
    {/if}
    {#each visible as reply (reply.entry_version_id)}
      <div class="flex min-w-0 items-start gap-2">
        <div class="shrink-0" aria-hidden="true"><AgentAvatar name={reply.author.label} class="h-7 w-7 rounded-lg text-xs" /></div>
        <div class="min-w-0 flex-1">
          <p class="text-xs text-muted">
            <span class="font-medium text-foreground">{reply.author.label}</span>{#if reply.author.kind === 'agent'}<span class="ml-1 rounded bg-accent-subtle px-1 py-px text-xs font-medium uppercase tracking-wide text-accent">Agent</span>{/if}
            · <time datetime={reply.created_at} title={formatAbsoluteTime(reply.created_at)}>{formatRelativeTime(reply.created_at)}</time>
          </p>
          <p class="whitespace-pre-wrap break-words text-sm">{reply.body}</p>
        </div>
      </div>
    {/each}
  </div>
{/if}
{#if onReply}
  {#if writing}
    <div class="mt-1.5">
      <label class="block"><span class="sr-only">Reply</span>
        <textarea class="w-full rounded-md border border-separator bg-canvas p-2 text-sm text-foreground focus:border-accent focus:outline-none" rows="2" maxlength="16384"
          placeholder="Reply…" bind:value={body} disabled={busy}
          onkeydown={(event) => { if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) { event.preventDefault(); void send(); } if (event.key === 'Escape') { event.stopPropagation(); writing = false; } }}></textarea>
      </label>
      {#if error}<p role="alert" class="text-xs text-danger">{error}</p>{/if}
      <div class="flex justify-end gap-1">
        <button type="button" class="feedback-action text-muted" onclick={() => { writing = false; body = ''; }}>Cancel</button>
        <button type="button" class="feedback-primary" disabled={busy || !body.trim()} onclick={send}>Reply</button>
      </div>
    </div>
  {:else}
    <button type="button" class="feedback-action -ml-2 text-muted hover:text-foreground" {disabled} onclick={() => writing = true}>Reply</button>
  {/if}
{/if}
