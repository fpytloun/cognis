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
    onOngoingWorkChange,
    showQueuedMessages = true,
    showTodoDrawer = true,
  } = $props<{
    onQueueChange?: (messages: Array<Record<string, unknown>>) => void;
    onTodosChange?: (todos: Array<Record<string, unknown>>) => void;
    onOngoingWorkChange?: (work: Array<Record<string, unknown>>) => void;
    showQueuedMessages?: boolean;
    showTodoDrawer?: boolean;
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
    onOngoingWorkChange?.([{
      kind: 'delegated_session',
      work_id: 'session-child',
      controller_conversation_id: 'conversation-one',
      session_id: 'session-child',
      title: 'Investigate visa delivery',
      agent_id: 'riker',
      status: 'running',
      started_at: '2026-09-10T07:50:00Z',
      updated_at: '2026-09-10T07:51:00Z',
      todos: [],
    }]);
  });
</script>

<div class="min-h-0 flex-1" data-testid="compact-timeline-fixture" data-show-queue={showQueuedMessages} data-show-todo-drawer={showTodoDrawer} data-admissions={admissions}>
  Timeline
  {#if showQueuedMessages}<p>Queued once</p>{/if}
</div>
