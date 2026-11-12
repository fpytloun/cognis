<script lang="ts">
  import { feedbackApi } from '$lib/feedback/api';
  import type { SubmissionReceipt } from '$lib/feedback/types';
  import { libraryHref } from '$lib/library';
  import ConversationLink from '$lib/components/ConversationLink.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  let { receipt }: { receipt: SubmissionReceipt } = $props();
  let checked = $state<SubmissionReceipt | null>(null);
  const current = $derived(checked?.submission_id === receipt.submission_id ? checked : receipt);
  let detail = $state<Awaited<ReturnType<typeof feedbackApi.submission>> | null>(null);
  let results = $state<Awaited<ReturnType<typeof feedbackApi.results>> | null>(null);
  let error = $state('');
  let busy = $state(false);
  const DELIVERY = { pending: 'Sending…', accepted: 'Sent', rejected: 'Not delivered', uncertain: 'Delivery unknown' } as const;
  const WORK = { queued: 'waiting for the agent', working: 'agent is working', waiting_for_input: 'agent is waiting for you',
    completed: 'done', failed: 'agent failed', cancelled: 'cancelled', unavailable: '' } as const;
  const status = $derived([DELIVERY[current.delivery], current.delivery === 'accepted' ? WORK[current.work] : ''].filter(Boolean).join(' · '));
  async function inspect() {
    busy = true;
    try { [detail, results] = await Promise.all([feedbackApi.submission(current.submission_id), feedbackApi.results(current.submission_id)]); }
    catch { error = 'The sent comments are unavailable with your current access.'; }
    finally { busy = false; }
  }
</script>
<section class="space-y-1 py-2 text-sm" aria-label="Feedback submission">
  <p>{status}</p>
  <div class="flex flex-wrap items-center gap-x-3">
    {#if current.admission?.kind === 'conversation_turn'}<ConversationLink class="inline-flex min-h-11 items-center text-accent" conversationId={current.admission.conversation_id}>Open conversation</ConversationLink>{/if}
    {#if current.admission?.kind === 'task_attempt'}<a class="inline-flex min-h-11 items-center text-accent" href={`/tasks/${encodeURIComponent(current.admission.task_id)}`}>Open task</a>{/if}
    {#if current.check_delivery_available}<Button variant="ghost" size="sm" class="text-accent" disabled={busy} onclick={async () => { busy = true; try { checked = await feedbackApi.check(current.submission_id); } catch (caught) { error = caught instanceof Error ? caught.message : 'Could not reconcile delivery.'; } finally { busy = false; } }}>Check delivery</Button>{/if}
    {#if !detail}<Button variant="ghost" size="sm" class="text-muted" disabled={busy} onclick={inspect}>Show sent comments</Button>{/if}
  </div>
  {#if detail}
    {#each detail.manifest.entries.filter(item => item.role === 'instruction') as item}<p class="whitespace-pre-wrap border-l-2 border-separator pl-2">{item.entry.body}</p>{/each}
    {#if detail.manifest.overall_message}<p class="whitespace-pre-wrap text-muted">Note: {detail.manifest.overall_message}</p>{/if}
    {#each results?.items ?? [] as result}<a class="inline-flex min-h-11 items-center text-accent" href={libraryHref({ kind: result.content_ref.startsWith('dlv_') ? 'deliverable' : 'artifact', id: result.content_ref })}>Open {result.relation.replaceAll('_', ' ')}</a>{/each}
  {/if}
  {#if error}<p role="alert" class="text-xs text-danger">{error}</p>{/if}
</section>
