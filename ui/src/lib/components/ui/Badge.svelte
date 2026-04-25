<script lang="ts">
  import type { Snippet } from 'svelte';

  import { cn } from '$lib/utils';

  let { class: className = '', children } = $props<{ class?: string; children?: Snippet }>();
</script>

<span class={cn('inline-flex items-center rounded-full border border-[color:var(--theme-border)] bg-[color:var(--theme-panel-muted)] px-2.5 py-1 text-xs font-medium text-[color:var(--theme-text)]', className)}>
  {@render children?.()}
</span>
