<script lang="ts">
  import { onMount, untrack } from 'svelte';
  import RefreshCw from 'lucide-svelte/icons/refresh-cw';
  import { auth } from '$lib/stores/auth';
  import { loadProviderMonitor, needsReauthorization, providerMonitoring } from '$lib/provider-monitor';
  import ProviderUsageBars from './ProviderUsageBars.svelte';

  let { providerId, showName = true, compact = true, heading, showUsage = true }: { providerId: string; showName?: boolean; compact?: boolean; heading?: string; showUsage?: boolean } = $props();
  let busy = $state(false);
  let error = $state('');
  let now = $state(Date.now());
  const value = $derived($providerMonitoring[providerId]);
  const stamp = $derived(value?.usage?.fetched_at ?? value?.usage?.observed_at ?? value?.health_checked_at);
  const expired = $derived(Boolean(value?.stale || (stamp && now - Date.parse(stamp) > 3_600_000)));
  const plan = $derived((value?.usage?.plan_type ?? '').trim());
  const showPlan = $derived(Boolean(plan) && !['unknown', 'common', 'default', 'none'].includes(plan.toLowerCase()));
  const status = $derived(value?.alert_severity ?? (value?.error ? 'warning' : value?.stale ? 'unknown' : value?.health_status ?? 'unknown'));
  const colors: Record<string, string> = { healthy: 'bg-success', warning: 'bg-warning', critical: 'bg-danger', error: 'bg-danger', unknown: 'bg-muted' };

  async function refresh(force = false): Promise<void> {
    const id = providerId;
    busy = true;
    error = '';
    try { await loadProviderMonitor(id, force); }
    catch { if (id === providerId) error = 'Refresh failed. Try again.'; }
    finally { if (id === providerId) busy = false; }
  }

  $effect(() => {
    const id = providerId;
    if (id) untrack(() => void refresh());
  });

  onMount(() => {
    const timer = window.setInterval(() => {
      if (!document.hidden) { now = Date.now(); void refresh(); }
    }, 60_000);
    return () => window.clearInterval(timer);
  });
</script>

<div class="min-w-0 text-xs" data-testid={`provider-health-${providerId}`}>
  <div class={`${compact ? 'mb-0' : 'mb-1'} flex min-w-0 items-center gap-2`}>
    <span class={`h-1.5 w-1.5 shrink-0 rounded-full ${colors[status] ?? colors.unknown}`} role="img" aria-label={`Provider health: ${status}`} title={value?.health_detail ?? status}></span>
    <div class="min-w-0 flex-1">
      {#if heading}
        <p class="text-[10px] font-semibold uppercase tracking-widest text-quiet">{heading}</p>
      {:else if showName}
        <a class="block truncate font-medium text-accent hover:text-accent-hover" href={`/settings?tab=providers&provider=${encodeURIComponent(providerId)}`}>{value?.display_name ?? providerId}</a>
      {/if}
    </div>
    {#if compact && showPlan}<span class="shrink-0 text-[10px] text-quiet">Plan {plan}</span>{/if}
    {#if value && !value.monitoring.enabled}<span class="shrink-0 text-[10px] text-quiet" title="Automatic checks disabled">auto off</span>{/if}
    {#if $auth.user?.role !== 'viewer'}
      <button type="button" class="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded text-quiet hover:bg-surface-hover hover:text-foreground focus-visible:outline focus-visible:outline-accent disabled:opacity-40"
        disabled={busy} onclick={() => void refresh(true)} aria-label={`Refresh ${value?.display_name ?? providerId}`}
        title={stamp ? `Refresh now · Refreshed ${new Date(stamp).toLocaleString()}${expired ? ' · stale' : ''}` : 'Refresh now · No successful refresh'}>
        <RefreshCw class={`h-3 w-3 ${busy ? 'motion-safe:animate-spin' : ''}`} />
      </button>
    {/if}
  </div>
  {#if showUsage && value?.usage?.ok}
    {#if !compact && showPlan}<p class="mb-3 text-xs text-muted">Plan {plan}</p>{/if}
    <ProviderUsageBars usage={value.usage} {compact} />
  {/if}
  {#if (expired && stamp) || error || value?.error}
    <div class="mt-1.5 flex flex-wrap gap-x-2 text-[10px]">
      {#if expired && stamp}
        <span class="text-warning" title={new Date(stamp).toLocaleString()}>
          Refreshed {new Date(stamp).toLocaleString()} · stale
        </span>
      {/if}
      {#if !error && needsReauthorization(value)}
        <a class="text-danger underline" role="alert" href={`/settings?tab=providers&provider=${encodeURIComponent(providerId)}`}>{value?.health_detail}</a>
      {:else if error || value?.error}<span class="text-warning" role="status">{error || value?.error}</span>{/if}
    </div>
  {/if}
  {#if !compact && value?.usage?.usage_url}
    <a class="mt-2 inline-block text-xs text-accent hover:text-accent-hover underline" href={value.usage.usage_url} target="_blank" rel="noreferrer">Open provider usage dashboard</a>
  {/if}
</div>
