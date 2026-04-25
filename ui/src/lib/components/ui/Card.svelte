<script lang="ts">
  import type { Snippet } from 'svelte';

  import { cn } from '$lib/utils';

  let { class: className = '', children } = $props<{ class?: string; children?: Snippet }>();
</script>

<div class={cn('rounded-3xl border border-slate-800/80 bg-slate-900/80 shadow-card backdrop-blur', className)}>
  {@render children?.()}
</div>
