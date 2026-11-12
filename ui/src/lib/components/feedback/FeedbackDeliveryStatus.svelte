<script lang="ts">
  import type { FeedbackDeliveryModel } from '$lib/feedback/delivery';
  import FeedbackChromeMount from './FeedbackChromeMount.svelte';
  import { deliveryChrome } from './delivery-chrome';
  import Button from '$lib/components/ui/Button.svelte';
  let { model }: { model: FeedbackDeliveryModel } = $props();
  const snapshot = $derived(model.state);
  let opened = $state(false);
</script>
{#if opened}
  <FeedbackChromeMount definition={deliveryChrome} viewProps={{ model, conversationId: model.conversationId }} onClose={() => { opened = false; }} />
{:else}
  {#each $snapshot.pending as entry}
    <section class="border-t border-separator py-2 text-xs" aria-label="Feedback delivery">
      <p>Message with comments not confirmed yet</p>
      <button type="button" class="min-h-11 text-accent" onclick={() => { opened = true; void model.check(entry); }}>Check delivery</button>
      <Button variant="ghost" size="sm" class="text-accent" onclick={() => { opened = true; }}>Show message</Button>
    </section>
  {/each}
{/if}
