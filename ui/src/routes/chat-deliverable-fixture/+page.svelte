<script lang="ts">
  import { page } from '$app/state';
  import ChatMessage from '$lib/components/ChatMessage.svelte';
  import AssistantDeliverableBlock from '$lib/components/AssistantDeliverableBlock.svelte';
  import TaskWorkPanel from '$lib/components/task-cockpit/TaskWorkPanel.svelte';
  import type { Deliverable } from '$lib/types/api';
  import type { MessageTimelineItem } from '$lib/timeline-render-model';
  import { renderMarkdown } from '$lib/markdown';
  import { onMount } from 'svelte';
  import { auth } from '$lib/stores/auth';
  import ChatV2Composer from '$lib/components/chat-v2/ChatV2Composer.svelte';
  import AttachmentSaveButton from '$lib/components/AttachmentSaveButton.svelte';
  import { messageMarkdown } from '../../../e2e/fixtures/chat-markdown';
  import ToastViewport from '$lib/components/ToastViewport.svelte';
  import { addToast } from '$lib/stores/toasts';
  import Button from '$lib/components/ui/Button.svelte';

  let hydrated = $state(false);
  let revealed = $state(false);
  onMount(() => {
    hydrated = true;
    if (page.url.searchParams.has('favorites')) void auth.bootstrap();
  });

  const taskList = '- [ ] Pending task\n- [x] Completed task\n  - Ordinary child\n  - [ ] Nested task\n- Ordinary parent\n  - [x] Nested completed task\n\n- [ ] Loose task\n\n  Extra paragraph.\n\n- Ordinary loose item';
  const deliverable: Deliverable = {
    deliverable_id: 'chat-flow-review',
    title: 'Release readiness',
    format: 'rich',
    step_run_id: null, version: 2, attempt_number: 1, target: null,
    outputs: {}, status: 'approved', evaluator_feedback: null,
    created_at: null, updated_at: null,
    content: 'The release is ready for staging. Verify recovery before production.',
    revision: 2, latest_revision: 2, is_latest: true,
    lineage_id: 'chat-flow-review', latest_deliverable_id: 'chat-flow-review',
    rich_payload: {
      blocks: [
        ...(page.url.searchParams.has('tasks') ? [{ type: 'markdown', content: taskList }] : []),
        { type: 'hero', title: 'Release readiness', subtitle: 'The implementation is ready for staging. Recovery remains the production gate.',
          badges: page.url.searchParams.has('tones') ? ['neutral', 'info', 'success', 'warning', 'danger'].map(tone => ({ label: tone, tone })) : [] },
        { type: 'markdown', content: '## What changed\n\nThe new connection handler preserves the current conversation during a reconnect. Pending messages stay queued until the server confirms that the session is ready.\n\nThis keeps the common path simple without hiding failures. Operators can distinguish a temporary network interruption from an authorization error.' },
        { type: 'table', title: 'Acceptance checks', columns: ['Area', 'Evidence', 'Result'], rows: [
          ['Message ordering', 'Replay matches the live event stream', 'Passed'],
          ['Reconnect', 'Queued messages delivered once', 'Passed'],
          ['Recovery', 'Staging restart exercise', 'Pending'],
        ] },
        { type: 'callout', tone: 'warning', title: 'Before production', content: 'Run the recovery exercise in staging and confirm that pending messages survive a restart.' },
        ...(page.url.searchParams.has('tones') ? ['neutral', 'info', 'success', 'danger'].map(tone => ({
          type: 'callout', tone, title: `${tone} status`, content: 'Semantic status with readable text and a subdued surface.',
        })) : []),
      ],
      metadata: { toc: { enabled: true } },
    },
  };
  const introductionText = page.url.searchParams.has('markdown') ? messageMarkdown : page.url.searchParams.has('tasks') ? taskList : 'The reconnect change is complete. Here is the release assessment and the one remaining check.';
  const introduction: MessageTimelineItem = {
    id: 'chat-flow-introduction', kind: 'message', role: 'assistant',
    content: introductionText, html: renderMarkdown(introductionText), seq: null,
    timestamp: '2026-01-01T12:00:00Z', attachments: [],
    commentReplies: page.url.searchParams.has('comment-replies') ? [
      { callId: 'reply-simplicity', commentId: 'comment-simplicity', body: 'Agreed. Kept the implementation **simple**, using the existing cleanup configuration instead of another refactor.', resolved: true },
      { callId: 'reply-coverage', commentId: 'comment-coverage', body: 'The completed sweep covered files eligible at its cutoff. New files can become eligible afterwards, so scheduled cleanup still matters.', resolved: true },
      { callId: 'reply-growth', commentId: 'comment-growth', body: 'Disk growth and cleanup eligibility are different measurements. The report now distinguishes the two.', resolved: false },
    ] : undefined,
  };
</script>

<svelte:head><title>Chat deliverable presentation</title></svelte:head>
<div class:chat-shell={page.url.searchParams.has('chrome')}>
{#if page.url.searchParams.has('chrome')}<header class="fixture-header">Riker</header>{/if}
<main class="h-screen overflow-y-auto overflow-x-hidden bg-canvas text-foreground">
  {#if page.url.searchParams.has('save-error')}
    <div class="p-4"><AttachmentSaveButton filename="preview.txt" resolveUrl={async () => null} /></div>
  {/if}
  <section class="mx-auto w-full px-4 py-8 sm:px-8" class:max-w-5xl={!page.url.searchParams.has('wide')} data-testid="chat-deliverable-preview">
    <ChatMessage item={introduction} />
    {#if page.url.searchParams.has('markdown')}
      <ChatMessage item={{ ...introduction, id: 'chat-markdown-user', role: 'user' }} />
      <Button onclick={() => addToast('Conversation created', 'success')}>Show notification</Button>
      <ToastViewport />
    {/if}
    {#if page.url.searchParams.has('client')}
      <button type="button" data-testid="show-client-deliverable" disabled={!hydrated} onclick={() => { revealed = true; }}>Show deliverable</button>
    {/if}
    {#if !page.url.searchParams.has('markdown') && (!page.url.searchParams.has('client') || revealed) && (!page.url.searchParams.has('api') || $auth.initialized)}
    {#if page.url.searchParams.has('inline-task')}
    <TaskWorkPanel stepRuns={[]} finalDeliverable={{ ...deliverable, step_run_id: 'fixture-final-step' }}
      canonicalDeliverableId={deliverable.deliverable_id}
      view={page.url.searchParams.has('activity') ? 'all' : 'deliverable'} deliverableCollapsedByDefault={false} />
    {:else}
    <AssistantDeliverableBlock collapsedByDefault={page.url.searchParams.has('work')}
      item={{ id: 'chat-flow-output', kind: 'assistant_deliverable', deliverableId: deliverable.deliverable_id, format: 'rich', title: deliverable.title,
        deliverable: page.url.searchParams.has('inline') ? deliverable : undefined }}
      loadDeliverable={page.url.searchParams.has('api') || page.url.searchParams.has('inline') ? undefined : async () => deliverable}
    />
    {/if}
    {/if}
  </section>
</main>
{#if page.url.searchParams.has('chrome')}
  <footer class="fixture-composer" data-testid="chat-composer-host">
    <ChatV2Composer placeholder="Message Riker" onSend={() => {}} onStop={() => {}} onFiles={() => {}} />
  </footer>
{/if}
</div>

<style>
  .chat-shell { display: flex; flex-direction: column; height: 100dvh; overflow: hidden; }
  .chat-shell main { height: auto; min-height: 0; flex: 1; }
  .fixture-header { flex: none; padding: 20px 16px; border-bottom: 1px solid rgb(var(--color-separator)); background: rgb(var(--color-canvas)); font-weight: 600; }
  .fixture-composer { flex: none; padding: 12px 16px max(12px, var(--app-safe-area-bottom, 0px)); background: rgb(var(--color-canvas)); }
</style>
