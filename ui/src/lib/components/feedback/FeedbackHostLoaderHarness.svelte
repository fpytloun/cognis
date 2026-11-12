<script lang="ts">
  import FeedbackHost from './FeedbackHost.svelte';
  import FeedbackIndicator from './FeedbackIndicator.svelte';
  import type { FeedbackContext, FeedbackSourceRef } from '$lib/feedback/types';
  let { source = { kind: 'document', content_ref: 'art_87d6238b098f4798919dc4b299894379' },
    context = { kind: 'chat', conversation_id: 'loader-conversation' } }: {
    source?: FeedbackSourceRef; context?: FeedbackContext;
  } = $props();
</script>
<FeedbackHost {source} {context}>
  <FeedbackIndicator />
</FeedbackHost>
