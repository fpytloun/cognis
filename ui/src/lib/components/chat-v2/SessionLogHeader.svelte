<script lang="ts">
  import type { Snippet } from 'svelte';

  let { title, testId, leading, metadata, actions } = $props<{
    title: string;
    testId: string;
    leading?: Snippet;
    metadata?: Snippet;
    actions: Snippet;
  }>();
</script>

<header
  data-find-toolbar
  class="app-keyboard-stable-header chat-header-shell relative z-20 flex shrink-0 items-center gap-2 border-b border-separator px-3 py-2 sm:px-4"
  style="padding-left: max(0.75rem, env(safe-area-inset-left)); padding-right: max(0.75rem, env(safe-area-inset-right));"
  data-testid={testId}
>
  {#if leading}{@render leading()}{/if}
  <div class="min-w-0 flex-1">
    <h2 class="truncate text-sm font-semibold text-foreground">{title}</h2>
    {#if metadata}<div class="mt-0.5 min-w-0 text-xs text-muted">{@render metadata()}</div>{/if}
  </div>
  <div data-find-actions class="flex shrink-0 items-center gap-1">
    {@render actions()}
  </div>
</header>
