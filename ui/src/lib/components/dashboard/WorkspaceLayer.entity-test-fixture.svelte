<script lang="ts">
  let {
    conversationId = null,
  } = $props<{
    conversationId?: string | null;
  }>();

  let draft = $state('');
</script>

<textarea
  bind:value={draft}
  data-testid={`workspace-chat-draft-${conversationId}`}
  aria-label={`Draft ${conversationId}`}
></textarea>
