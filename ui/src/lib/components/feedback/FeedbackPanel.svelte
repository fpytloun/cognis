<script lang="ts">
  import type { FeedbackController } from '$lib/feedback/state';
  import FeedbackComments from './FeedbackComments.svelte';
  import FeedbackReview from './FeedbackReview.svelte';
  import FeedbackAccess from './FeedbackAccess.svelte';
  import FeedbackSubmission from './FeedbackSubmission.svelte';
  import { libraryHref } from '$lib/library';
  let { controller }: { controller: FeedbackController } = $props();
  const port = $derived(controller.presentation());
  const snapshot = $derived(controller.state);
</script>
{#snippet review(entries: import('$lib/feedback/types').FeedbackEntryVersion[], sources: import('$lib/feedback/review-selection').FeedbackReviewSource[])}
  {#if $snapshot.source}<FeedbackReview {entries} {sources} subjectIds={[$snapshot.source.subject_id]} context={port.context} onClose={() => port.openRail('threads')} embedded bar onSent={() => void port.reload()} />{/if}
{/snippet}
{#snippet access()}{#if $snapshot.source}<FeedbackAccess source={$snapshot.source} />{/if}{/snippet}
{#snippet history()}
  {#each $snapshot.history as item}
    <article class="border-b border-separator py-3 text-sm"><p>{item.kind} · {new Date(item.created_at).toLocaleString()}</p>
      {#if item.receipt}<FeedbackSubmission receipt={item.receipt} />{/if}
      {#if item.result_ref}<a class="inline-flex min-h-11 items-center text-accent" href={libraryHref({ kind: item.result_ref.startsWith('dlv_') ? 'deliverable' : 'artifact', id: item.result_ref })}>Open result revision</a>{/if}
      {#if item.state}<p>{item.state}</p>{/if}{#if item.reason}<p class="text-muted">{item.reason}</p>{/if}
    </article>
  {:else}<p class="py-4 text-sm text-muted">No feedback history yet.</p>{/each}
{/snippet}
<FeedbackComments {port} {review} {access} {history} />
