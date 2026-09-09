<script lang="ts">
  import { onDestroy } from 'svelte';
  import { mountConversationWorkspaceFixture } from './conversation-workspace-fixture-state';

  let {
    conversationId,
    sessionId = '',
    inspectorControlInHeader = false,
    inspectorOpen = true,
    onInspectorStateChange,
  } = $props<{
    conversationId: string;
    sessionId?: string;
    inspectorControlInHeader?: boolean;
    inspectorOpen?: boolean;
    onInspectorStateChange?: (open: boolean) => void;
  }>();
  const fixture = mountConversationWorkspaceFixture();
  const mountId = fixture.mountId;
  let draft = $state('');
  let inspectorTab = $state('overview');
  let scrollPosition = $state('640');
  let inspectorVisible = $state(true);

  $effect(() => {
    inspectorVisible = inspectorOpen;
  });

  onDestroy(fixture.destroy);

  export async function toggleInspector(open = !inspectorVisible): Promise<void> {
    inspectorVisible = open;
    onInspectorStateChange?.(open);
  }
  export function handleEscape(): boolean { return false; }
</script>

<div
  data-testid="conversation-modal-workspace"
  data-mount-id={mountId}
  data-conversation-id={conversationId}
  data-inspector-control-in-header={inspectorControlInHeader}
  data-inspector-open={inspectorVisible}
>
  <textarea data-testid="workspace-draft" bind:value={draft}></textarea>
  <input data-testid="workspace-scroll" bind:value={scrollPosition} />
  <select data-testid="workspace-inspector-tab" bind:value={inspectorTab}>
    <option value="overview">Overview</option>
    <option value="work">Work</option>
    <option value="session">Session</option>
  </select>
  <span data-testid="workspace-session">{sessionId}</span>
  <span data-testid="workspace-autotail">paused</span>
  <span data-testid="workspace-queue">queued:2</span>
  <span data-testid="workspace-outbox">pending:1</span>
</div>
