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
  const resets = $derived(windows.filter((item) => item.window.resets_at && Number.isFinite(Date.parse(item.window.resets_at))));
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

<div class={compact ? `grid min-w-0 ${windows.length >= 3 ? 'grid-cols-3' : 'grid-cols-2'} gap-x-0.5 gap-y-0.5` : 'space-y-3'} data-testid="provider-usage-bars">
  {#each windows as item (item.id)}
    <div class="min-w-0">
      <div class={`${compact ? 'mb-0.5 gap-0.5' : 'mb-1 gap-2'} flex min-w-0 items-baseline justify-between text-xs`}>
        <span class="truncate text-secondary" title={item.name}>{item.name}</span>
        <span class="shrink-0 tabular-nums text-foreground">{Math.round(percent(item.window.used_percent))}% used</span>
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
  {#if compact && resets.length}
    <div class={`min-w-0 self-end text-[10px] leading-[12px] text-quiet ${windows.length > 1 ? 'col-span-full flex flex-wrap gap-x-3 gap-y-0.5' : ''}`}
      aria-label="Limit reset times" data-testid="compact-limit-resets">
      {#each resets as item (item.id)}
        <p class="flex min-w-0 items-baseline gap-1" title={`${item.name} resets ${new Date(item.window.resets_at!).toLocaleString()}`}>
          <span class="truncate">{item.name} reset</span>
          <time class="shrink-0 tabular-nums" datetime={item.window.resets_at!}>{resetLabel(item.window.resets_at!)}</time>
        </p>
      {/each}
    </div>
  {/if}
</div>
