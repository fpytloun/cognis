<script lang="ts">
  import BottomTabBar from '$lib/components/BottomTabBar.svelte';
  import ResourceReader from '$lib/components/library/ResourceReader.svelte';
  import ConversationInfoDrawer from '$lib/components/ConversationInfoDrawer.svelte';
  import WorkView from '$lib/components/work/WorkView.svelte';
  import FeedbackContextProvider from './FeedbackContextProvider.svelte';
  import FeedbackHost from './FeedbackHost.svelte';
  import FeedbackIndicator from './FeedbackIndicator.svelte';
  import FeedbackWorkspace from './FeedbackWorkspace.svelte';
  import { adaptiveBottomInset } from '$lib/actions/adaptiveBottomInset';
  import type { LibraryItem } from '$lib/library';
  import { context, scope, projection } from '$lib/components/work/work-event-feedback.fixture';
  import type { ConversationInfoPresentation } from '$lib/stores/conversationInfo.svelte';
  let { mode, shell = true }: { mode: string; shell?: boolean } = $props();
  let presentation = $state<ConversationInfoPresentation>('pinned');
  const item: LibraryItem = {
    id: 'art_87d6238b098f4798919dc4b299894379', kind: 'artifact', title: 'release-review.txt',
    format: 'text/plain', owner_email: null, agent_id: null, agent_name: null, project_id: null,
    project_name: null, conversation_id: null, conversation_title: null, task_id: null, task_title: null,
    lineage_id: null, revision: 1, created_at: '2026-01-12T12:00:00Z', updated_at: '2026-01-12T12:00:00Z',
    size_bytes: 257, status: 'ready', origin: 'user', is_readonly: false,
  };
</script>

<div class="fixed inset-0 flex min-h-0 flex-col bg-canvas text-foreground">
  <header class="flex h-11 shrink-0 items-center border-b border-separator px-3">Library · native acceptance fixture</header>
  {#if mode === 'work'}
    <FeedbackContextProvider {context}>
      <div class="grid min-h-0 flex-1 grid-cols-[1fr_auto]">
        <aside class="p-4">Conversation source workspace</aside>
        <ConversationInfoDrawer open {presentation} width={340} onClose={() => undefined} onWidthChange={() => undefined} onWidthCommit={() => undefined}
          onFocusChange={focus => presentation = focus ? 'focus' : 'pinned'}>
          <WorkView {scope} live={false} loadWork={async () => projection()} refreshWork={async () => undefined}
            onSourceFocus={() => { if (presentation === 'pinned') presentation = 'focus'; }} />
        </ConversationInfoDrawer>
      </div>
    </FeedbackContextProvider>
  {:else if mode === 'embedded'}
    <FeedbackContextProvider context={{ kind: 'chat', conversation_id: 'native-transcript-fixture' }}>
      <main class="ml-40 min-h-0 flex-1 overflow-auto p-4">
        {#each [1, 2] as sequence}
          <FeedbackHost source={{ kind: 'transcript', conversation_id: 'native-transcript-fixture',
            source_event: { store: 'intaris', session_id: 'native-fixture-session', seq: sequence, event_type: 'assistant_message' },
            source_item_id: `native-message-${sequence}`, field_path: ['content'] }}>
            <FeedbackWorkspace>
              <FeedbackIndicator variant="compact" /><FeedbackIndicator />
              <p>Ordinary message {sequence}</p>
            </FeedbackWorkspace>
          </FeedbackHost>
        {/each}
      </main>
    </FeedbackContextProvider>
  {:else}
    <main use:adaptiveBottomInset={{ disabled: !shell }} class="min-h-0 flex-1 overflow-y-auto p-3">
      <div class="flex h-full min-h-0 flex-col"><ResourceReader {item} /></div>
    </main>
    {#if shell}<BottomTabBar />{/if}
  {/if}
</div>
