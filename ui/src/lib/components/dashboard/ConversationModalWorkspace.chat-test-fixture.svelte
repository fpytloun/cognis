<script lang="ts">
  let { conversationId, timelineScope, onViewSession }: {
    conversationId: string;
    timelineScope?: { key: string };
    onViewSession?: (sessionId: string, node?: Record<string, unknown>) => void;
  } = $props();
  let draft = $state('');
</script>

<input data-testid={`compact-chat-draft-${conversationId}`} bind:value={draft} />
<span data-testid="compact-chat-scope">{timelineScope?.key}</span>
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
