<script lang="ts">
  import { Activity, AlertTriangle, ArrowUpRight, Calendar, Check, Clock, ExternalLink, Info, TrendingDown, TrendingUp } from 'lucide-svelte';

  export let icon: unknown;
  export let label = '';

  const namedIcons: Record<string, typeof Activity> = {
    activity: Activity,
    alert: AlertTriangle,
    calendar: Calendar,
    check: Check,
    clock: Clock,
    external: ExternalLink,
    info: Info,
    trend_down: TrendingDown,
    trend_up: TrendingUp,
    arrow_up_right: ArrowUpRight,
  };

  $: value = typeof icon === 'string' ? icon.trim() : '';
  $: named = namedIcons[value.toLowerCase().replace(/[-\s]+/g, '_')];
  // Emoji and Unicode symbols are rendered as text. Named icons are intentionally
  // limited to the UI's vendored Lucide set; arbitrary markup is never accepted.
  $: symbol = !named && value && !/^[a-z0-9 _-]+$/i.test(value) ? value : '';
</script>

{#if named}
  <span class="rich-icon" aria-label={label || undefined} title={label || undefined} data-rich-icon={value}>
    <svelte:component this={named} size={18} strokeWidth={2} aria-hidden={!label} />
  </span>
{:else if symbol}
  <span class="rich-icon rich-icon-symbol" aria-label={label || undefined} title={label || undefined} data-rich-icon={symbol} aria-hidden={!label}>{symbol}</span>
{/if}

<style>
  .rich-icon {
    display: inline-grid;
    width: 2rem;
    height: 2rem;
    flex: 0 0 auto;
    place-items: center;
    border-radius: .7rem;
    background: color-mix(in srgb, var(--rich-accent) 12%, transparent);
    color: var(--rich-accent-soft);
  }

  .rich-icon-symbol {
    font-size: 1.1rem;
    line-height: 1;
  }
</style>
