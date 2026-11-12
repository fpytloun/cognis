<script lang="ts">
  import type { Snippet } from 'svelte';

  import Card from './Card.svelte';
  import { cn } from '$lib/utils';

  /** Titled data section built on the Card primitive. */
  let { title, label, actions, children, class: className = '' }: {
    title?: string;
    /** Accessible region name; defaults to the title. */
    label?: string;
    actions?: Snippet;
    children?: Snippet;
    class?: string;
  } = $props();
</script>

<Card class={cn('min-w-0 p-4 md:p-5', className)}>
  <section aria-label={label ?? title} class="min-w-0">
    {#if title || actions}
      <header class="mb-3 flex flex-wrap items-center justify-between gap-2">
        {#if title}<h2 class="m-0 text-sm font-semibold text-foreground">{title}</h2>{/if}
        {#if actions}<div class="flex flex-wrap items-center gap-2">{@render actions()}</div>{/if}
      </header>
    {/if}
    {@render children?.()}
  </section>
</Card>
