<script lang="ts">
  import Popover from '$lib/components/ui/Popover.svelte';
  import type { WorkstreamRef } from '$lib/chat-v2/types';
  import ActivityAvatar from './ActivityAvatar.svelte';

  let { node, name, avatarUrl = null, running = false } = $props<{
    node: WorkstreamRef;
    name: string;
    avatarUrl?: string | null;
    running?: boolean;
  }>();

  const kindLabel = $derived(
    node.key === node.root_key ? 'Main'
      : node.kind === 'managed' || node.kind === 'managed_agent' ? 'Managed'
      : node.kind === 'delegate' ? 'Delegate'
      : node.kind === 'task_step' || node.task_id ? 'Task step'
      : node.kind === 'source' ? 'Source'
      : node.kind,
  );
  const details = $derived([
    name,
    `ID: ${node.agent_id}`,
    `Profile: ${node.agent_profile_id ?? 'default'}`,
    `Model: ${node.model ?? 'Unavailable'}`,
    `Thinking: ${node.reasoning_effort ?? 'default'}`,
    `Kind: ${kindLabel}`,
  ].join('\n'));
</script>

<Popover text={details} placement="right">
  <button
    type="button"
    class="rounded-full focus:outline-none focus-visible:ring-2 focus-visible:ring-sky-400/60"
    aria-label={`Session identity: ${name}`}
    data-testid="activity-identity-control"
  >
    <ActivityAvatar {name} {avatarUrl} turnInProgress={running} class="h-6 w-6" />
  </button>
</Popover>
