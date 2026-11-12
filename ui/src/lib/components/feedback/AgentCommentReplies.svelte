<script lang="ts">
  import { renderMarkdown } from '$lib/markdown';
  import type { AgentCommentReply } from '$lib/feedback/agent-replies';
  import type { FeedbackThread } from '$lib/feedback/types';
  import { feedbackApi } from '$lib/feedback/api';
  import { documentCoordination } from '$lib/document-coordination';
  import { getCommentWorkspace } from '$lib/feedback/comment-workspace';
  import { revealComment } from '$lib/feedback/locate-target';
  import { targetSummary } from '$lib/feedback/target-summary';
  import { libraryHref } from '$lib/library';
  import TargetQuote from './TargetQuote.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  let { replies }: { replies: AgentCommentReply[] } = $props();
  const workspace = getCommentWorkspace();
  let comments = $state<Record<string, FeedbackThread>>({});
  let unavailable = $state<string[]>([]);
  let error = $state('');
  const commentIds = $derived([...new Set(replies.map(reply => reply.commentId))].join('\n'));
  $effect(() => {
    const ids = commentIds.split('\n').filter(Boolean);
    const lease = documentCoordination().capture();
    const request = new AbortController();
    let alive = true;
    comments = {}; unavailable = []; error = '';
    const stop = documentCoordination().onInvalidated(() => {
      alive = false; request.abort(); comments = {}; unavailable = []; error = '';
    });
    for (const id of ids) {
      feedbackApi.thread(id, request.signal).then(thread => {
        lease.assertCurrent();
        if (alive) comments = { ...comments, [id]: thread };
      }).catch(() => { if (alive) unavailable = [...unavailable, id]; });
    }
    return () => { alive = false; request.abort(); stop(); };
  });
  async function locate(thread: FeedbackThread) {
    error = '';
    const found = workspace ? await workspace.locate(thread)
      : thread.anchor.source.kind !== 'document' && await revealComment({ source: thread.anchor.source, locator: thread.anchor.locator });
    if (!found) error = 'The annotation is not available in this view. Open the original revision to inspect it.';
  }
</script>

<!-- Separate from the source message's selectable text: these answers belong to their threads. -->
<section aria-label="Comment replies" class="mt-4 space-y-3 border-t border-separator pt-3" data-copy-exclude>
  <h3 class="text-xs font-medium text-muted">Comment replies</h3>
  {#each replies as reply (reply.callId)}
    {@const thread = comments[reply.commentId]}
    <div class="min-w-0">
      {#if thread?.entries[0]}
        {@const entry = thread.entries[0]}
        <div class="mb-2 rounded-lg border border-separator bg-surface/60 p-2">
          <TargetQuote summary={targetSummary({ locator: thread.anchor.locator, source: thread.anchor.source, sourceTitle: null })}
            onLocate={() => locate(thread)} />
          <Button variant="link" size="inline" class="mt-1 block w-full select-text text-left text-foreground hover:no-underline" aria-label={`Locate comment: ${entry.body}`} onclick={() => locate(thread)}>
            <span class="block text-xs font-medium text-muted">{entry.author.label}</span>
            <span class="block whitespace-pre-wrap break-words text-sm">{entry.body}</span>
          </Button>
          {#if thread.anchor.source.kind === 'document'}
            <a class="mt-1 inline-block text-xs text-accent" href={libraryHref({ kind: thread.anchor.source.content_ref.startsWith('dlv_') ? 'deliverable' : 'artifact', id: thread.anchor.source.content_ref })}>Open original revision</a>
          {/if}
        </div>
      {:else}
        <p class="mb-1 text-xs text-muted">{unavailable.includes(reply.commentId) ? 'Original comment unavailable' : 'Loading original comment…'}</p>
      {/if}
      {#if reply.resolved}<p class="mb-1 text-xs text-muted">Resolved</p>{/if}
      <div class="chat-markdown prose max-w-none overflow-x-auto break-words [overflow-wrap:anywhere]">
        {@html renderMarkdown(reply.body)}
      </div>
    </div>
  {/each}
  {#if error}<p role="status" class="text-xs text-muted">{error}</p>{/if}
</section>
