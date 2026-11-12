<script lang="ts">
  import ToolCallBlock from '$lib/components/ToolCallBlock.svelte';
  import PythonExecutionDetails from '$lib/components/chat-v2/PythonExecutionDetails.svelte';
  import type { TimelineScope } from '$lib/chat-v2/types';
  import type { ToolCallTimelineItem } from '$lib/timeline-render-model';

  let {
    item,
    getToolCall = () => null,
    onViewSession,
    scope
  } = $props<{
    item: ToolCallTimelineItem;
    getToolCall?: (callId: string) => ToolCallTimelineItem | null;
    onViewSession?: ((sessionId: string) => void | Promise<void>) | undefined;
    scope?: TimelineScope | undefined;
  }>();

  const nestedCalls = $derived(item.programChildren ?? []);
  const waitingApproval = $derived(nestedCalls.some((call: ToolCallTimelineItem) => call.status === 'waiting'));
  const status = $derived(waitingApproval && ['started', 'running'].includes(item.status) ? 'waiting' : item.status);
  const parentItem = $derived({ ...item, status, displayToolName: 'Python execution' });
</script>

<div
  class="min-w-0"
  data-kind="python_execution"
  data-call-id={item.callId}
  data-program-id={item.programId ?? undefined}
  data-tool-status={item.status}
  data-python-execution-status={status}
>
  <ToolCallBlock item={parentItem} {getToolCall} {onViewSession} {scope}>
    {#snippet detailsContent()}
      {#if waitingApproval}
        <p class="text-xs font-medium text-warning" role="status">
          Waiting for approval
        </p>
      {/if}
      <PythonExecutionDetails
        {item}
        {nestedCalls}
        {getToolCall}
        {onViewSession}
        {scope}
      />
    {/snippet}
  </ToolCallBlock>
</div>
