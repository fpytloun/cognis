<script lang="ts">
  import CompactConversationChat from '$lib/components/chat-v2/CompactConversationChat.svelte';
  import type { Agent, TaskControlChatResponse } from '$lib/types/api';
  let { chat, agent = null, onSent, onRuntimeActiveChange }: {
    chat: TaskControlChatResponse; agent?: Agent | null;
    onSent?: () => void | Promise<void>; onRuntimeActiveChange?: (active: boolean) => void;
  } = $props();
</script>

<!-- Task-control chats share the coordinated draft and admission lifecycle. -->
<section class="h-full min-h-0" aria-label="Task control chat" data-testid="task-control-native-chat">
  <CompactConversationChat conversationId={chat.conversation_id} {agent} {onSent} {onRuntimeActiveChange}
     emptyLabel="Start a task-control conversation." composerAriaLabel="Task control message"
     composerTestId="task-control-composer" sendAriaLabel="Send task control message" embedded />
</section>
