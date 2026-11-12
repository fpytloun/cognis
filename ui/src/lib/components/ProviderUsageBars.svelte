<script lang="ts">
  import type { CodexUsageWindow, ProviderUsage } from '$lib/types/api';

  let { usage, compact = false }: { usage: ProviderUsage; compact?: boolean } = $props();
  function label(window: CodexUsageWindow, position: string): string {
    const minutes = window.window_duration_mins ?? 0;
    if (minutes === 300) return compact ? '5-hour' : '5-hour rolling limit';
    if (minutes >= 10080) return compact ? 'Weekly' : 'Weekly rolling limit';
    if (minutes >= 1440) return compact ? 'Long-term' : 'Long-term rolling limit';
    return position === 'primary' ? 'Short-term' : 'Secondary';
  }
  const windows = $derived([
    ...(usage.primary ? [{ id: 'primary', name: label(usage.primary, 'primary'), window: usage.primary }] : []),
    ...(usage.secondary ? [{ id: 'secondary', name: label(usage.secondary, 'secondary'), window: usage.secondary }] : []),
    ...usage.additional_rate_limits.flatMap((limit, index) => [
      ...(limit.primary ? [{ id: `additional-${index}-primary`, name: limit.limit_name ?? limit.limit_id ?? 'Additional limit', window: limit.primary }] : []),
      ...(limit.secondary ? [{ id: `additional-${index}-secondary`, name: `${limit.limit_name ?? limit.limit_id ?? 'Additional limit'} · ${label(limit.secondary, 'secondary')}`, window: limit.secondary }] : [])
    ])
  ]);
  function resetLabel(timestamp: string): string {
    const date = new Date(timestamp);
    return `${date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })} ${date.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', hourCycle: 'h23' })}`;
  }
  function percent(value: number): number { return Number.isFinite(value) ? Math.min(100, Math.max(0, value)) : 0; }
  function color(value: number): string {
    if (value > 95) return 'bg-danger';
    if (value > 85) return 'bg-danger/80';
    if (value > 65) return 'bg-warning';
    return 'bg-success';
  }
</script>

<div class={compact ? 'min-w-0 space-y-1.5' : 'space-y-3'} data-testid="provider-usage-bars">
  {#each windows as item (item.id)}
    {@const reset = compact && item.window.resets_at && Number.isFinite(Date.parse(item.window.resets_at)) ? item.window.resets_at : null}
    <div class="min-w-0">
      <div class="mb-1 flex min-w-0 flex-wrap items-baseline gap-x-2 text-xs">
        <span class="min-w-0 break-words text-secondary">{item.name}</span>
        {#if reset}
          <!-- Keep the reset on the label row so each compact limit stays two lines tall. -->
          <span class="flex items-baseline gap-x-1 text-quiet" title={`${item.name} resets ${new Date(reset).toLocaleString()}`}>
            <span>Resets</span>
            <time class="tabular-nums" datetime={reset}>{resetLabel(reset)}</time>
          </span>
        {/if}
        <span class="ml-auto shrink-0 tabular-nums text-foreground">{Math.round(percent(item.window.used_percent))}% used</span>
      </div>
      <div class={compact ? 'h-1.5 overflow-hidden rounded-full bg-raised' : 'h-2 overflow-hidden rounded-full bg-raised'}
        role="progressbar" aria-label={`${item.name} usage`} aria-valuemin="0" aria-valuemax="100" aria-valuenow={percent(item.window.used_percent)}
        title={item.window.resets_at ? `Resets ${new Date(item.window.resets_at).toLocaleString()}` : 'Reset time unavailable'}>
        <div class={`h-full rounded-full ${color(item.window.used_percent)}`} style={`width: ${percent(item.window.used_percent)}%`}></div>
      </div>
      {#if !compact}
        {#if typeof item.window.remaining === 'number' && typeof item.window.limit === 'number'}
          <p class="mt-1 text-xs text-muted">{item.window.remaining.toLocaleString()} / {item.window.limit.toLocaleString()} left</p>
        {/if}
        <p class="mt-1 text-xs text-quiet">{item.window.resets_at ? `Resets ${new Date(item.window.resets_at).toLocaleString()}` : 'Reset time unavailable'}</p>
      {/if}
    </div>
  {/each}
</div>
