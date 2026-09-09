<script lang="ts">
  import { onMount } from 'svelte';
  let admissions = $state(0);
  export async function reconcileMessageAdmission(): Promise<boolean> {
    admissions += 1;
    return true;
  }

  let {
    onQueueChange,
    onTodosChange,
    showQueuedMessages = true,
  } = $props<{
    onQueueChange?: (messages: Array<Record<string, unknown>>) => void;
    onTodosChange?: (todos: Array<Record<string, unknown>>) => void;
    showQueuedMessages?: boolean;
  }>();

  onMount(() => {
    onQueueChange?.([{
      queue_id: 'queue-one',
      content: 'Queued once',
      created_at: '2026-08-26T00:00:00Z',
      attachments: [],
    }]);
    onTodosChange?.([{
      content: 'Keep the dashboard stable',
      status: 'in_progress',
    }]);
  });
</script>

<div class="min-h-0 flex-1" data-testid="compact-timeline-fixture" data-show-queue={showQueuedMessages} data-admissions={admissions}>
  Timeline
  {#if showQueuedMessages}<p>Queued once</p>{/if}
</div>
