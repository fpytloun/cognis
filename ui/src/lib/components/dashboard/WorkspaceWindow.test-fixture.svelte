<script lang="ts">
  import WorkspaceWindow from './WorkspaceWindow.svelte';
  import type { WorkspaceManager, WorkspaceWindowState } from '$lib/dashboard/workspace';

  let {
    window,
    manager,
    largeControls = false,
    inspectorExpanded,
    inspectorControlsId,
    onToggleInspector,
  } = $props<{
    window: WorkspaceWindowState;
    manager: WorkspaceManager;
    largeControls?: boolean;
    inspectorExpanded?: boolean;
    inspectorControlsId?: string;
    onToggleInspector?: () => void;
  }>();
</script>

<WorkspaceWindow {window} {manager} {largeControls} {inspectorExpanded} {inspectorControlsId} {onToggleInspector}>
  {#snippet children()}
    <input data-testid="workspace-child-state" value="preserved" />
  {/snippet}
</WorkspaceWindow>
