<script lang="ts">
  import type { Snippet } from 'svelte';
  import { provideChatMessageFeedback } from '$lib/feedback/chat-feedback-context';
  let { sourceKey, transcript = false, children }: { sourceKey: string; transcript?: boolean; document?: boolean; children: Snippet } = $props();
  provideChatMessageFeedback({ get eligible() { return transcript; }, get sourceKey() { return sourceKey; } });
</script>
<div class="contents">
  {@render children()}
</div>
