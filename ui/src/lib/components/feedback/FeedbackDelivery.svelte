<script lang="ts">
  import { onMount, untrack } from 'svelte';
  import { FeedbackDeliveryModel } from '$lib/feedback/delivery';
  import ConversationLink from '$lib/components/ConversationLink.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  let { conversationId, model: provided }: { conversationId: string; model?: FeedbackDeliveryModel } = $props();
  const model = untrack(() => provided ?? new FeedbackDeliveryModel(conversationId));
  const state = model.state;
  onMount(() => provided ? undefined : model.start());
</script>
{#each $state.pending as entry}
  <section class="border-t border-separator py-2 text-xs" aria-label="Feedback delivery">
    <p>Message with comments not confirmed yet{$state.receipts[entry.client_txn_id] ? ` · ${$state.receipts[entry.client_txn_id].delivery}` : ''}</p>
    <button type="button" class="min-h-11 text-accent" disabled={$state.busy} onclick={() => model.check(entry)}>Check delivery</button>
    {#if $state.missing.includes(entry.client_txn_id)}<p>The message did not arrive. Retrying sends the same message once.</p><Button variant="ghost" size="sm" class="text-accent" disabled={$state.busy} onclick={() => model.retryOriginal(entry)}>Retry original message</Button>{/if}
    <details><summary class="min-h-11 cursor-pointer py-3">Show message</summary><p class="whitespace-pre-wrap">{entry.content || 'Comments only'}</p>{#if entry.attachments.length}<p>{entry.attachments.length} attachments</p>{/if}</details>
  </section>
{/each}
{#if Object.values($state.receipts).some(receipt => receipt.delivery === 'accepted')}<ConversationLink class="inline-flex min-h-11 items-center text-xs text-accent" {conversationId} reload>Sent · Open conversation</ConversationLink>{/if}
{#if $state.error}<p role="alert" class="text-xs text-danger">{$state.error}</p>{/if}
