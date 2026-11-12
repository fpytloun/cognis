<script lang="ts">
  import FeedbackHost from './FeedbackHost.svelte';
  import FeedbackIndicator from './FeedbackIndicator.svelte';
  import HtmlPreview from '../HtmlPreview.svelte';
  import type { HtmlPreviewSource } from '$lib/attachments/html-preview';
  import type { FeedbackContext, FeedbackSourceRef } from '$lib/feedback/types';
  let { preview, source, context }: { preview: HtmlPreviewSource; source: FeedbackSourceRef; context: FeedbackContext } = $props();
</script>
<FeedbackHost {source} {context}>
  <FeedbackIndicator />
  <HtmlPreview source={preview} />
</FeedbackHost>
