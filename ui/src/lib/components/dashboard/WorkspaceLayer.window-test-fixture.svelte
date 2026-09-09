<script lang="ts">
  import type { Snippet } from 'svelte';
  import type { WorkspaceManager, WorkspaceWindowState } from '$lib/dashboard/workspace';

  let {
    window,
    children,
  } = $props<{
    window: WorkspaceWindowState;
    manager: WorkspaceManager;
    children: Snippet;
  }>();

  const instanceId = crypto.randomUUID();
</script>

<div
  data-testid={`workspace-window-${window.key}`}
  data-window-key={window.key}
  data-instance-id={instanceId}
  data-minimized={window.minimized}
>
  {@render children()}
</div>
