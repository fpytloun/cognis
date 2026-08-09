<script lang="ts">
  import ConversationInfoDrawer from './ConversationInfoDrawer.svelte';
  import type { ConversationInfoPresentation } from '$lib/stores/conversationInfo.svelte';

  let { presentation = 'pinned' } = $props<{ presentation?: ConversationInfoPresentation }>();
  let currentPresentation = $state<ConversationInfoPresentation>('pinned');
  let width = $state(512);
  $effect(() => { currentPresentation = presentation; });
</script>

<ConversationInfoDrawer
  open
  presentation={currentPresentation}
  {width}
  onWidthChange={(next) => { width = next; }}
  onWidthCommit={(next) => { width = next; }}
  onFocusChange={(focus) => { currentPresentation = focus ? 'focus' : 'pinned'; }}
  onClose={() => {}}
>
  <p>Inspector content</p>
</ConversationInfoDrawer>
