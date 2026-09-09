<script lang="ts">
  import AgentAvatar from '$lib/components/AgentAvatar.svelte';
  import DashboardModalShell from '$lib/components/dashboard/DashboardModalShell.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import ConversationModalWorkspace from '$lib/components/dashboard/ConversationModalWorkspace.svelte';
  import { api, asApiError } from '$lib/api/client';
  import type { Agent } from '$lib/types/api';

  /**
   * Quick chat modal opened from an agent avatar in the Control Center
   * strip. Resolves (or creates) the agent-direct conversation for the
   * clicked agent, then reuses the same compact conversation chat as the
   * entity modal.
   */

  let {
    agent,
    onClose,
    embedded = false,
    onResolved,
    onConversationInitialLoaded,
    inspectorOpen = true,
    inspectorControlInHeader = false,
    onInspectorStateChange,
  } = $props<{
    agent: Agent;
    onClose: () => void;
    embedded?: boolean;
    onResolved?: (conversationId: string) => void;
    onConversationInitialLoaded?: (conversationId: string) => void | Promise<void>;
    inspectorOpen?: boolean;
    inspectorControlInHeader?: boolean;
    onInspectorStateChange?: (open: boolean) => void;
  }>();

  let loading = $state(true);
  let error = $state<string | null>(null);
  let conversationId = $state<string | null>(null);
  let sessionId = $state('');
  let boundAgentId = '';
  let conversationWorkspace = $state<{
    handleEscape(): boolean;
    toggleInspector(open?: boolean): Promise<void>;
  } | null>(null);

  export function handleEscape(): boolean {
    return conversationWorkspace?.handleEscape() ?? false;
  }

  export function toggleInspector(open = !inspectorOpen): Promise<void> | undefined {
    return conversationWorkspace?.toggleInspector(open);
  }

  async function resolve(): Promise<void> {
    loading = true;
    error = null;
    try {
      const conversation = await api.conversations.resolve({
        agent_id: agent.agent_id,
        scope: 'agent_direct'
      });
      conversationId = conversation.conversation_id;
      sessionId = conversation.active_session_id ?? '';
      onResolved?.(conversation.conversation_id);
    } catch (caught) {
      error = asApiError(caught).message || 'Could not open this agent.';
    } finally {
      loading = false;
    }
  }

  $effect(() => {
    if (agent.agent_id === boundAgentId) return;
    boundAgentId = agent.agent_id;
    conversationId = null;
    void resolve();
  });
</script>

<DashboardModalShell
  title={agent.display_name ?? agent.name}
  {onClose}
  testId="agent-quick-chat-modal"
  {embedded}
  onEscape={handleEscape}
>
    <div class="flex min-h-0 flex-1 flex-col overflow-hidden" data-testid="agent-quick-chat-modal-body">
      {#if loading}
        <div class="flex flex-1 flex-col items-center justify-center gap-3 p-6 text-center text-sm text-slate-400">
          <AgentAvatar name={agent.display_name ?? agent.name} avatarUrl={agent.avatar_url} class="h-10 w-10" />
          <p>Opening…</p>
        </div>
      {:else if error}
        <div class="m-4 rounded-xl border border-rose-500/30 bg-rose-500/10 p-4 text-sm text-rose-100" role="alert">
          <p>{error}</p>
          <Button class="mt-3" size="sm" variant="secondary" onclick={() => void resolve()}>Try again</Button>
        </div>
      {:else if conversationId}
        <ConversationModalWorkspace
          bind:this={conversationWorkspace}
          {conversationId}
          {sessionId}
          {agent}
          agents={[agent]}
          onInitialLoaded={onConversationInitialLoaded}
          {inspectorOpen}
          {inspectorControlInHeader}
          {onInspectorStateChange}
        />
      {/if}
    </div>
</DashboardModalShell>
