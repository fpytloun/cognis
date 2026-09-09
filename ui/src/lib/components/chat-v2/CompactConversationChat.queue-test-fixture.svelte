<script lang="ts">
  let { messages = [] } = $props<{ messages?: Array<{ content?: string }> }>();
</script>

{#if messages.length > 0}
  <div class="h-40" data-testid="editable-queue-fixture">
    {#each messages as message}<p>{message.content}</p>{/each}
  </div>
{/if}
