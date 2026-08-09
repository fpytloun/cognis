<script lang="ts">
  import ConversationInfoDrawer from '$lib/components/ConversationInfoDrawer.svelte';
  import WorkFileTree from '$lib/components/work/WorkFileTree.svelte';
  import {
    INSPECTOR_DEFAULT_WIDTH,
    INSPECTOR_MIN_WIDTH,
    type ConversationInfoPresentation,
  } from '$lib/stores/conversationInfo.svelte';

  let shell = $state<HTMLElement | null>(null);
  let shellWidth = $state(0);
  let open = $state(true);
  let focus = $state(false);
  let width = $state(INSPECTOR_DEFAULT_WIDTH);
  let canPin = $derived(shellWidth - 512 - 16 >= INSPECTOR_MIN_WIDTH);
  let presentation = $derived<ConversationInfoPresentation>(
    !open ? 'closed' : focus ? 'focus' : canPin ? 'pinned' : 'overlay',
  );

  $effect(() => {
    if (!shell) return;
    const observer = new ResizeObserver(([entry]) => { shellWidth = entry.contentRect.width; });
    observer.observe(shell);
    return () => observer.disconnect();
  });

  const diffs = [{
    path: 'src/a-very-long-file-name-that-must-not-overflow-the-workspace.ts',
    diff: '@@ -1 +1 @@\n-old\n+new',
  }];
</script>

<main bind:this={shell} class="flex h-screen min-w-0 overflow-hidden bg-slate-950" data-testid="workspace-fixture">
  <section class={`min-w-0 flex-1 ${presentation === 'pinned' ? 'grid grid-cols-[minmax(32rem,1fr)_auto]' : 'flex'}`}>
    <div class="min-w-0 overflow-x-hidden p-4" data-testid="fixture-chat">
      <p class="break-words">chat-{`x`.repeat(300)}</p>
      {#if presentation === 'overlay'}
        <button type="button" onclick={() => { open = true; }}>Open inspector</button>
      {/if}
    </div>
    <ConversationInfoDrawer
      {open}
      {presentation}
      {width}
      onWidthChange={(next) => { width = next; }}
      onWidthCommit={(next) => { width = next; }}
      onFocusChange={(next) => { focus = next; }}
      onClose={() => { open = false; }}
    >
      <WorkFileTree {diffs} />
    </ConversationInfoDrawer>
  </section>
</main>
