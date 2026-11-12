<script lang="ts">
  import type { Snippet } from 'svelte';
  import ChevronDown from 'lucide-svelte/icons/chevron-down';
  import ChevronUp from 'lucide-svelte/icons/chevron-up';

  let {
    id,
    title,
    collapsed = $bindable(false),
    status,
    children,
    footer,
  } = $props<{
    id: string;
    title: string;
    collapsed?: boolean;
    status?: Snippet;
    children: Snippet;
    footer?: Snippet;
  }>();
</script>

<section class="flex max-h-[min(50dvh,calc(var(--app-viewport-height,100dvh)-12rem))] min-h-0 flex-col overflow-hidden rounded-2xl border border-info-border bg-info-subtle text-sm text-foreground shadow-card">
  <div class="flex shrink-0 items-center justify-between gap-3 px-4 py-2">
    <button
      type="button"
      class="flex min-h-11 min-w-0 flex-1 items-center gap-2 rounded-xl text-left hover:text-accent focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent"
      aria-expanded={!collapsed}
      aria-controls={id}
      onclick={() => { collapsed = !collapsed; }}
    >
      {#if collapsed}<ChevronDown class="h-4 w-4 shrink-0 text-info" aria-hidden="true" />
      {:else}<ChevronUp class="h-4 w-4 shrink-0 text-info" aria-hidden="true" />{/if}
      <span class="truncate font-semibold">{title}</span>
    </button>
    {#if status}<div class="flex shrink-0 items-center gap-2">{@render status()}</div>{/if}
  </div>
  <!-- Keep form controls mounted so collapsing never discards unsent input. -->
  <div {id} hidden={collapsed} class="min-h-0 flex-1 flex-col overflow-hidden" class:flex={!collapsed}>
    <div class="min-h-0 flex-1 overflow-y-auto overscroll-contain border-t border-info-border px-4 py-3">
      {@render children()}
    </div>
    {#if footer}
      <div class="shrink-0 border-t border-info-border px-4 py-3">{@render footer()}</div>
    {/if}
  </div>
</section>
