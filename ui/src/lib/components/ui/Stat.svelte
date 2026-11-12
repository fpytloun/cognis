<script lang="ts">
  import type { Snippet } from 'svelte';

  import Card from './Card.svelte';
  import { cn } from '$lib/utils';

  export type StatTone = 'neutral' | 'warning' | 'danger' | 'unknown';

  /** One headline number with a single baseline line. Optional click target. */
  let { label, value, baseline, tone = 'neutral', title, onclick, children }: {
    label: string;
    value: string;
    baseline?: string;
    tone?: StatTone;
    /** Exact value on hover. */
    title?: string;
    onclick?: () => void;
    children?: Snippet;
  } = $props();

  const valueTone = $derived({ neutral: 'text-foreground', warning: 'text-warning', danger: 'text-danger', unknown: 'text-secondary' }[tone]);
  const frame = $derived(tone === 'warning' ? 'border-warning-border bg-warning-subtle' : tone === 'danger' ? 'border-danger-border bg-danger-subtle' : '');
</script>

{#snippet body()}
  <span class="text-xs font-semibold uppercase tracking-wide text-secondary">{label}</span>
  <strong class={cn('text-2xl font-semibold tabular-nums leading-tight', valueTone)} {title}>{value}</strong>
  {@render children?.()}
  {#if baseline}<span class="text-xs tabular-nums text-secondary">{baseline}</span>{/if}
{/snippet}

<Card class={cn('min-w-0', frame)}>
  {#if onclick}
    <button type="button" class="grid w-full gap-1 rounded-3xl p-4 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent" aria-label={`${label}: ${value}${baseline ? `, ${baseline}` : ''}`} {onclick}>{@render body()}</button>
  {:else}
    <div class="grid gap-1 p-4" role="group" aria-label={label}>{@render body()}</div>
  {/if}
</Card>
