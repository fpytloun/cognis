<script lang="ts">
  import ClipboardList from 'lucide-svelte/icons/clipboard-list';
  import Button from '$lib/components/ui/Button.svelte';
  import DeferredPrivateView from '$lib/components/ui/DeferredPrivateView.svelte';
  import { dashboardEntityModal } from '$lib/components/ui/private-view-definitions';
  import type { TaskOutcomeReference } from '$lib/chat-v2/types';

  let { task, handling = false, label }: { task: TaskOutcomeReference; handling?: boolean; label?: string } = $props();
  let open = $state(false);
</script>

<div class={`my-2 text-xs text-secondary ${handling ? 'px-3' : ''}`} data-testid={handling ? 'task-handling-row' : label ? 'task-outcome-action' : 'task-outcome-footer'}>
  <Button variant="link" size="inline" class="max-w-full items-start gap-1.5 text-xs" onclick={() => { open = true; }}>
    <ClipboardList size={14} class="mt-0.5 shrink-0" aria-hidden="true" />
    <span class="min-w-0 text-left break-words">
      {#if label}{label}
      {:else if handling}Handling task: {task.title} ({task.status})
      {:else}{task.title} · Attempt {task.attempt_number}{/if}
    </span>
  </Button>
</div>

{#if open}
  <DeferredPrivateView definition={dashboardEntityModal}
    viewProps={{ kind: 'task', taskId: task.task_id, onClose: () => { open = false; } }}
    label="task" modal onClose={() => { open = false; }} />
{/if}
