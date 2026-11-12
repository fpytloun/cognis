<script lang="ts">
  import { onDestroy, untrack } from 'svelte';
  import DeferredPrivateView from '../ui/DeferredPrivateView.svelte';
  import { fileDiffViewer } from '../ui/private-view-definitions';
  import type { FileDiff } from '$lib/diff';
  import { FeedbackDiffRegistry } from '$lib/feedback/diff-registry';
  import { getFeedbackContext } from '$lib/feedback/context';
  import FeedbackDiffWorkspace from './FeedbackDiffWorkspace.svelte';
  let { diffs, collapsible = true, collapsedByDefault = false, onExpand }: {
    diffs: FileDiff[]; collapsible?: boolean; collapsedByDefault?: boolean; onExpand?: () => void;
  } = $props();
  const origin = getFeedbackContext();
  let revision = $state(0);
  let contextKey = untrack(() => JSON.stringify(origin.context));
  let registry = $state(untrack(() => new FeedbackDiffRegistry(origin.context, () => { revision++; }, () => origin.context)));
  $effect(() => {
    const next = JSON.stringify(origin.context);
    if (next === contextKey) return;
    registry.dispose(); contextKey = next;
    registry = new FeedbackDiffRegistry(origin.context, () => { revision++; }, () => origin.context);
  });
  const error = $derived(registry.error);
  const annotationBindings = $derived.by(() => {
    void revision;
    return diffs.map(diff => registry.binding(diff.feedback_source));
  });
  onDestroy(() => registry.dispose());
</script>
<FeedbackDiffWorkspace {registry}>
  <div class="min-w-0">
    {#if $error}<p role="alert">{$error}</p><button type="button" class="min-h-11 text-accent" onclick={() => registry.retry()}>Retry feedback</button>{/if}
    <DeferredPrivateView definition={fileDiffViewer} viewProps={{ diffs, collapsible, collapsedByDefault, onExpand, annotationBindings }} label="file changes" />
  </div>
</FeedbackDiffWorkspace>
