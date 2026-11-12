<script lang="ts">
  let {
    conversationId,
    timelineScope,
    controllerSessionIds = [],
    onCommandResponse,
    onViewSession,
    onRuntimeActiveChange,
    agent,
  }: {
    conversationId: string;
    timelineScope?: { key: string };
    controllerSessionIds?: string[];
    onCommandResponse?: (response: { data?: Record<string, unknown> }) => void;
    onViewSession?: (sessionId: string, node?: Record<string, unknown>) => void;
    onRuntimeActiveChange?: (active: boolean) => void;
    agent?: { agent_id: string } | null;
  } = $props();
  let draft = $state('');
</script>

<input data-testid={`compact-chat-draft-${conversationId}`} bind:value={draft} />
<span data-testid="compact-chat-scope">{timelineScope?.key}</span>
<span data-testid="compact-chat-controller-sessions">{controllerSessionIds.join(',')}</span>
<span data-testid="compact-chat-agent">{agent?.agent_id ?? ''}</span>
<button onclick={() => onRuntimeActiveChange?.(false)}>Runtime idle</button>
<button onclick={() => onViewSession?.('session-delayed')}>View unresolved session</button>
{#each ['conversation-managed-a', 'conversation-managed-b'] as conversationId}
  <button onclick={() => onViewSession?.(`session-${conversationId}`, {
    key: `conversation:${conversationId}`, kind: 'managed', root_key: 'session:root',
    parent_key: 'session:root', session_id: `session-${conversationId}`, conversation_id: conversationId,
    agent_id: 'agent-1', title: conversationId, status: 'running',
    backing_session_ids: [`session-${conversationId}`],
  })}>View {conversationId}</button>
{/each}
<button
  type="button"
  onclick={() => onCommandResponse?.({
    data: { conversation_id: conversationId, session_id: 'session-rotated' },
  })}
>Rotate session</button>
<button
  type="button"
  onclick={() => onViewSession?.('session-child', {
    key: 'child',
    root_key: 'root',
    parent_key: 'root',
    kind: 'delegate',
    title: 'Child session',
    session_id: 'session-child',
    conversation_id: 'conversation-one',
    agent_id: 'agent',
    status: 'completed',
    activity_state: 'idle',
  })}
>View child session</button>
