<script lang="ts">
  import { provideFeedbackSurface } from '$lib/feedback/context';
  import type { FeedbackSurfaceBinding } from '$lib/feedback/types';
  import FeedbackIndicator from './FeedbackIndicator.svelte';
  let { commentCount }: { commentCount?: number | null } = $props();
  provideFeedbackSurface({
    get commentCount() { return commentCount; },
    binding: {
      highlights: [{}],
      source: { capabilities: { comment: true } },
      mode: 'view',
    } as FeedbackSurfaceBinding,
    open: () => undefined,
  });
</script>
<FeedbackIndicator />
