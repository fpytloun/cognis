<script lang="ts">
  import type { MessageTimelineItem, TimelineScope } from '$lib/chat-v2/types';
  import { toRenderItem } from '$lib/chat-v2/render-adapter';
  import { renderMarkdown } from '$lib/markdown';
  import { provideFeedbackContext } from '$lib/feedback/context';
  import type { FeedbackController } from '$lib/feedback/state';
  import ChatMessage from '../ChatMessage.svelte';
  import ChatFeedbackIntent from '../chat-v2/ChatFeedbackIntent.svelte';
  import FeedbackSourceProvider from './FeedbackSourceProvider.svelte';
  import FeedbackInline from './FeedbackInline.svelte';

  let { controller, item, scope }: { controller: FeedbackController; item: MessageTimelineItem; scope: TimelineScope } = $props();
  provideFeedbackContext({ get context() { return controller.context; } });
  const snapshot = $derived(controller.state), presenter = $derived(controller.presenter.state);
  const binding = $derived.by(() => { void $presenter; return controller.binding($snapshot); });
  const rendered = $derived(toRenderItem(item, content => renderMarkdown(content)));
</script>

<FeedbackSourceProvider {binding} available onOpen={() => {}}
  onCommenting={enabled => controller.patch({ mode: enabled ? 'comment' : 'view' })}>
  <ChatFeedbackIntent sourceKey={JSON.stringify([scope, controller.sourceRef])} transcript>
    {#if rendered?.kind === 'message'}<ChatMessage item={rendered} />{/if}
  </ChatFeedbackIntent>
</FeedbackSourceProvider>
<FeedbackInline {controller} />
