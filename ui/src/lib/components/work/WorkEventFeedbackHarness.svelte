<script lang="ts">
  import { onDestroy, type ComponentProps } from 'svelte';
  import { provideFeedbackContext } from '$lib/feedback/context';
  import { CommentWorkspace, provideCommentWorkspace } from '$lib/feedback/comment-workspace';
  import CommentControl from '$lib/components/feedback/CommentControl.svelte';
  import FeedbackConversationWorkspace from '$lib/components/feedback/FeedbackConversationWorkspace.svelte';
  import type { FeedbackContext } from '$lib/feedback/types';
  import WorkView from './WorkView.svelte';
  let { context, ...props }: ComponentProps<typeof WorkView> & { context: FeedbackContext } = $props();
  provideFeedbackContext({ get context() { return context; } });
  const workspace = new CommentWorkspace();
  provideCommentWorkspace(workspace);
  const state = workspace.state;
  $effect(() => workspace.reset(JSON.stringify(context)));
  onDestroy(() => workspace.dispose());
</script>
<CommentControl open={$state.enabled} onShow={() => { if ($state.enabled) workspace.setEnabled(false); else { workspace.setEnabled(true); workspace.showComments(); } }} />
<WorkView {...props} />
{#if context.kind === 'chat' && $state.active}
  <FeedbackConversationWorkspace {workspace} conversationId={context.conversation_id} />
{/if}
