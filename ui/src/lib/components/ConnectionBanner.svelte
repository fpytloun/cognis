<script lang="ts">
  import { onDestroy, tick } from 'svelte';
  import WifiOff from 'lucide-svelte/icons/wifi-off';
  import RefreshCw from 'lucide-svelte/icons/refresh-cw';
  import ChevronDown from 'lucide-svelte/icons/chevron-down';
  import { connectivity, retryConnectivityNow, type ConnectivityLevel } from '$lib/connectivity';

  // The single app-wide connectivity notice. While it applies, sections hide
  // their own connectivity errors (see $lib/connectivity).
  //
  // Brief reconnects (network handover, app resume) are normal on mobile and
  // recover silently. Only surface the banner when the outage persists.
  const SHOW_AFTER_MS = 2_500;

  let { showAfterMs = SHOW_AFTER_MS } = $props<{ showAfterMs?: number }>();

  let visible = $state(false);
  let expanded = $state(false);
  let truncated = $state(false);
  let labelElement = $state<HTMLSpanElement | null>(null);
  let showTimer: ReturnType<typeof setTimeout> | null = null;

  const LABELS: Record<Exclude<ConnectivityLevel, 'ok'>, string> = {
    offline: 'You are offline. Messages are saved and sent when the connection returns.',
    restarting: 'Cognis is restarting. Reconnecting automatically…',
    stalled: 'Connection lost. Retrying in the background.',
    unreachable: 'Cognis is not responding. Retrying…',
    reconnecting: 'Reconnecting…',
  };

  const status = $derived($connectivity);
  const label = $derived(status.level === 'ok' ? '' : LABELS[status.level]);
  const details = $derived.by(() => {
    const lines: string[] = [];
    if (status.level !== 'offline' && status.lastFailure) lines.push(`Last error: ${status.lastFailure}`);
    if ((status.level === 'reconnecting' || status.level === 'stalled') && status.attempts > 0) {
      lines.push(`Reconnect attempt ${status.attempts}`);
    }
    lines.push('Unsent messages are kept and sent automatically.');
    return lines;
  });

  function clearShowTimer(): void {
    if (showTimer !== null) clearTimeout(showTimer);
    showTimer = null;
  }

  $effect(() => {
    if (!status.degraded) {
      clearShowTimer();
      visible = false;
      expanded = false;
      return;
    }
    if (visible || showTimer !== null) return;
    showTimer = setTimeout(() => {
      showTimer = null;
      visible = true;
    }, showAfterMs);
  });

  // Offer expansion only when the one-line label does not fit.
  $effect(() => {
    const element = labelElement;
    void label;
    if (!element || expanded) return;
    const measure = () => { truncated = element.scrollWidth > element.clientWidth + 1; };
    void tick().then(measure);
    if (typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  });

  onDestroy(clearShowTimer);
</script>

{#if visible && status.degraded}
  <div
    class="flex justify-center px-3 pb-2"
    style="padding-top: calc(var(--app-safe-area-top, 0px) + 0.5rem); padding-left: var(--app-overlay-inner-left, 0.75rem); padding-right: var(--app-overlay-inner-right, 0.75rem);"
    data-testid="connection-banner"
    data-level={status.level}
  >
    <!-- Never wider than the screen (minus the side padding): the label is
         truncated to one line and can be expanded to its full text. -->
    <div
      class="pointer-events-auto flex min-w-0 max-w-full flex-col border border-warning-border bg-elevated/95 text-xs text-warning shadow-lg backdrop-blur sm:max-w-md"
      class:rounded-full={!expanded}
      class:rounded-2xl={expanded}
      role="status"
      aria-live="polite"
    >
      <div class="flex min-w-0 items-center gap-2 py-1.5 pl-3 pr-1.5">
        <WifiOff class="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
        <span
          bind:this={labelElement}
          class="min-w-0 flex-1"
          class:truncate={!expanded}
          data-testid="connection-banner-label"
        >{label}</span>
        {#if truncated || expanded}
          <button
            type="button"
            class="flex h-11 w-11 shrink-0 items-center justify-center rounded-full text-warning hover:bg-surface-hover"
            aria-expanded={expanded}
            aria-label={expanded ? 'Show less' : 'Show more'}
            data-testid="connection-banner-expand"
            onclick={() => { expanded = !expanded; }}
          >
            <ChevronDown class={`h-3.5 w-3.5 transition-transform ${expanded ? 'rotate-180' : ''}`} aria-hidden="true" />
          </button>
        {/if}
        {#if status.level !== 'offline'}
          <button
            type="button"
            class="flex min-h-11 shrink-0 items-center gap-1 rounded-full bg-warning-subtle px-2.5 py-1 font-medium text-warning hover:bg-surface-hover"
            onclick={() => retryConnectivityNow()}
          >
            <RefreshCw class="h-3 w-3" aria-hidden="true" />
            Retry
          </button>
        {/if}
      </div>
      {#if expanded}
        <ul class="space-y-1 border-t border-warning-border px-3 py-2 text-[11px] text-secondary" data-testid="connection-banner-details">
          {#each details as line}<li class="break-words">{line}</li>{/each}
        </ul>
      {/if}
    </div>
  </div>
{/if}
