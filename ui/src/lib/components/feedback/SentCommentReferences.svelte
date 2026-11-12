<script lang="ts">
  import { feedbackApi } from '$lib/feedback/api';
  import { targetSummary } from '$lib/feedback/target-summary';
  import { revealComment } from '$lib/feedback/locate-target';
  import { getCommentWorkspace } from '$lib/feedback/comment-workspace';
  const workspace = getCommentWorkspace();
  import type { FeedbackSubmissionDetail } from '$lib/feedback/types';
  import TargetQuote from './TargetQuote.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  /** The comments a sent message included, so the user can see and revisit what they commented on. */
  let { submissionId }: { submissionId: string } = $props();
  let detail = $state<FeedbackSubmissionDetail | null>(null), failed = $state(false), open = $state(false);
  $effect(() => {
    const id = submissionId;
    let alive = true;
    detail = null; failed = false;
    feedbackApi.submission(id).then(value => { if (alive) detail = value; }).catch(() => { if (alive) failed = true; });
    return () => { alive = false; };
  });
  const items = $derived(detail?.manifest.entries.filter(item => item.role === 'instruction') ?? []);
  // Like attachments, sent comments are visible; only a long list folds after the first three.
  const LIMIT = 3;
  const shown = $derived(open ? items : items.slice(0, LIMIT));
</script>
{#if items.length}
  <div class="mt-2 text-xs" data-copy-exclude aria-label={`${items.length === 1 ? '1 comment' : `${items.length} comments`} included`} role="group">
    <ul class="space-y-1.5">
      {#each shown as item (item.entry.entry_version_id)}
        <li class="rounded-lg border border-current/15 px-2 py-1.5">
          <TargetQuote summary={targetSummary({ locator: item.anchor.locator, source: item.anchor.source, sourceTitle: null })}
            onLocate={() => void revealComment({ source: item.anchor.source, locator: item.anchor.locator }, workspace ? source => workspace.reveal(source) : undefined)} />
          <p class="mt-0.5 line-clamp-3 whitespace-pre-wrap break-words pl-2.5">{item.entry.body}</p>
        </li>
      {/each}
    </ul>
    {#if items.length > LIMIT}
      <Button variant="ghost" size="inline" class="mt-1 text-xs text-inherit opacity-80 hover:opacity-100" aria-expanded={open} onclick={() => open = !open}>
        {open ? 'Show fewer' : `Show all ${items.length} comments`}
      </Button>
    {/if}
  </div>
{:else if failed}
  <p class="mt-2 text-xs opacity-70" data-copy-exclude>Comments included</p>
{/if}
