<script lang="ts">
  import type { Snippet } from 'svelte';
  import { provideFeedbackContext } from '$lib/feedback/context';
  import type { FeedbackContext } from '$lib/feedback/types';
  let { context, children }: { context: FeedbackContext; children: Snippet } = $props();
  provideFeedbackContext({ get context() { return context; } });
</script>
{@render children()}
