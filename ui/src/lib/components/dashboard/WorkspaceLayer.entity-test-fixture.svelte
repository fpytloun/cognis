<script lang="ts">
  import type { Agent } from '$lib/types/api';

  let {
    conversationId = null,
    agent = null,
  } = $props<{
    conversationId?: string | null;
    agent?: Agent | null;
  }>();

  let draft = $state('');
  const entityId = $derived(conversationId ?? agent?.agent_id ?? null);
</script>

<textarea
  bind:value={draft}
  data-testid={`workspace-chat-draft-${entityId}`}
  aria-label={`Draft ${entityId}`}
></textarea>
