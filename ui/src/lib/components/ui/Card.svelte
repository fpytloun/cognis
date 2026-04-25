<script lang="ts">
  import type { Snippet } from 'svelte';

  import { cn } from '$lib/utils';

  let { class: className = '', children } = $props<{ class?: string; children?: Snippet }>();
</script>

<div class={cn('rounded-3xl border border-[color:var(--theme-border)] bg-[color:var(--theme-panel)] shadow-card backdrop-blur', className)}>
  {@render children?.()}
</div>
