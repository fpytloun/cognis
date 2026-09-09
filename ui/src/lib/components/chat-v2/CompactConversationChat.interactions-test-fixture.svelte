<script lang="ts">
  let {
    conversationId,
    presentation = 'regular',
  }: {
    conversationId: string;
    presentation?: 'regular' | 'floating';
  } = $props();
</script>

<div
  class="h-40"
  data-testid="pending-interactions-fixture"
  data-conversation-id={conversationId}
  data-presentation={presentation}
>Pending approval</div>
