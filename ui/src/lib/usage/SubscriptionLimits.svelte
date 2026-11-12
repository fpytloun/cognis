<script lang="ts">
  import Panel from '$lib/components/ui/Panel.svelte';
  import { auth } from '$lib/stores/auth';
  import { statusPresentation } from '$lib/status';
  import { usageErrorMessage } from './adapter';
  import { loadSubscriptionLimits, type LimitWindow, type ProviderLimits } from './subscription-limits';
  import { seriesColors } from './bars';

  /** Compact: one row per provider (Overview). Detailed: one panel per provider with history. */
  let { start, end, detailed = false }: { start: string; end: string; detailed?: boolean } = $props();

  let load = $state<{ status: 'loading' } | { status: 'ready'; items: ProviderLimits[] } | { status: 'error'; message: string }>({ status: 'loading' });
  $effect(() => {
    const controller = new AbortController();
    load = { status: 'loading' };
    loadSubscriptionLimits(start, end, controller.signal, $auth.user?.role === 'admin' ? 'admin' : 'personal')
      .then((items) => { load = { status: 'ready', items }; })
      .catch((error) => { if (!controller.signal.aborted) load = { status: 'error', message: usageErrorMessage(error) }; });
    return () => controller.abort();
  });
  const items = $derived(load.status === 'ready' ? load.items : []);

  const level = (percent: number | null) => statusPresentation(percent == null ? 'unknown' : percent >= 90 ? 'failed' : percent >= 70 ? 'warning' : 'ready');
  function resets(at: string | null): string {
    if (!at) return '';
    const ms = Date.parse(at) - Date.now();
    if (!Number.isFinite(ms) || ms <= 0) return 'resets now';
    const hours = Math.floor(ms / 3_600_000), minutes = Math.round((ms % 3_600_000) / 60_000);
    return hours >= 48 ? `resets in ${Math.round(hours / 24)}d` : hours ? `resets in ${hours}h ${minutes}m` : `resets in ${minutes}m`;
  }
  const units = new Intl.NumberFormat('en', { notation: 'compact', maximumFractionDigits: 1 });
  const native = (window: LimitWindow) => window.used !== null && window.limit !== null ? `${units.format(window.used)} / ${units.format(window.limit)}` : '';

  // History chart: shared time axis = the selected range up to now, so sparse
  // sampling is visible as gaps instead of being stretched across the chart.
  const CH = 160, PAD = { left: 40, right: 12, top: 10, bottom: 24 };
  const axisDay = new Intl.DateTimeFormat('en', { month: 'short', day: 'numeric' });
  const axisHour = new Intl.DateTimeFormat('en', { hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });
  const span = $derived.by(() => {
    const t0 = Date.parse(start), t1 = Math.min(Date.parse(end), Date.now());
    return { t0, t1: Math.max(t1, t0 + 3_600_000) };
  });
  function chart(windows: LimitWindow[], width: number) {
    const w = Math.max(240, width), inner = w - PAD.left - PAD.right, h = CH - PAD.top - PAD.bottom;
    const { t0, t1 } = span;
    const x = (at: number) => PAD.left + Math.min(1, Math.max(0, (at - t0) / (t1 - t0))) * inner;
    const y = (v: number) => PAD.top + h - v / 100 * h;
    // A gap longer than an hour without observations breaks the line.
    const paths = windows.map((window) => {
      let d = '', last = -Infinity;
      for (const p of window.points) {
        if (p.at < t0 || p.at > t1) continue;
        d += `${p.at - last > 3_600_000 ? 'M' : 'L'}${x(p.at).toFixed(1)},${y(p.percent).toFixed(1)}`;
        last = p.at;
      }
      return d;
    });
    const hourly = t1 - t0 <= 2 * 86_400_000;
    const ticks = [0, .25, .5, .75, 1].map((part) => {
      const at = new Date(t0 + part * (t1 - t0));
      return { x: PAD.left + part * inner, label: hourly ? axisHour.format(at) : axisDay.format(at) };
    });
    return { w, y, paths, ticks };
  }
  let widths = $state<Record<string, number>>({});
</script>

{#if detailed}
  {#if load.status === 'loading'}
    <p class="rounded-3xl border border-dashed border-separator p-6 text-center text-sm text-secondary" role="status">Loading subscription limits…</p>
  {:else if load.status === 'error'}
    <p class="text-sm text-secondary" role="alert">{load.message}</p>
  {:else if !items.length}
    <p class="rounded-3xl border border-dashed border-separator p-6 text-center text-sm text-secondary">No subscription with usage limits is connected.</p>
  {:else}
    <div class="grid gap-4" role="region" aria-label="Subscription limits">
      {#each items as item (item.providerId)}
        {@const geometry = chart(item.windows, widths[item.providerId] ?? 0)}
        <Panel title={item.name} label={`${item.name} limits`}>
          {#snippet actions()}{#if item.plan}<span class="text-xs text-secondary">{item.plan}</span>{/if}{/snippet}
          <div class="mb-3 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
            {#each item.windows as window, index (window.key)}
              {@const status = level(window.current)}
              <div class="min-w-0">
                <div class="flex items-baseline justify-between gap-2 text-xs">
                  <span class="flex items-center gap-1.5 truncate text-secondary"><i class="inline-block h-0.5 w-3 rounded" style:background={seriesColors[index % seriesColors.length]}></i>{window.label}</span>
                  <b class={['text-base font-semibold tabular-nums', status.textClass]}>{window.current == null ? '—' : `${Math.round(window.current)}%`}</b>
                </div>
                <div class="relative mt-1 h-1.5 rounded-full bg-separator/60" role="meter" aria-valuemin={0} aria-valuemax={100} aria-valuenow={window.current ?? undefined} aria-label={`${item.name} ${window.label}`}>
                  <span class={['absolute inset-y-0 left-0 rounded-full', status.fillClass]} style:width={`${Math.min(100, window.current ?? 0)}%`}></span>
                </div>
                <div class="mt-1 flex justify-between gap-2 text-xs text-secondary tabular-nums"><span>{native(window)}</span><span>{resets(window.resetsAt)}</span></div>
              </div>
            {/each}
          </div>
          <div class="min-w-0" bind:clientWidth={() => widths[item.providerId] ?? 0, (value) => widths[item.providerId] = value}>
            {#if item.windows.some((window) => window.points.length)}
              <svg viewBox={`0 0 ${geometry.w} ${CH}`} height={CH} class="block w-full overflow-visible" role="img" aria-label={`${item.name} limit usage over the selected range`}>
                {#each [0, 50, 100] as tick}
                  <line x1={40} x2={geometry.w - 12} y1={geometry.y(tick)} y2={geometry.y(tick)} class="stroke-separator"/>
                  <text x={34} y={geometry.y(tick) + 3} text-anchor="end" class="fill-secondary text-[10px] tabular-nums">{tick}%</text>
                {/each}
                <line x1={40} x2={geometry.w - 12} y1={geometry.y(70)} y2={geometry.y(70)} class="stroke-warning/60" stroke-dasharray="4 4"/>
                <line x1={40} x2={geometry.w - 12} y1={geometry.y(90)} y2={geometry.y(90)} class="stroke-danger/60" stroke-dasharray="4 4"/>
                {#each geometry.paths as d, index}
                  {#if d}<path {d} fill="none" stroke={seriesColors[index % seriesColors.length]} stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>{/if}
                {/each}
                {#each geometry.ticks as tick, index}
                  <text x={tick.x} y={CH - 6} text-anchor={index === 0 ? 'start' : index === geometry.ticks.length - 1 ? 'end' : 'middle'} class="fill-secondary text-[10px] tabular-nums">{tick.label}</text>
                {/each}
              </svg>
            {:else}
              <p class="py-6 text-center text-sm text-secondary">No history in this range</p>
            {/if}
          </div>
        </Panel>
      {/each}
    </div>
    <p class="mt-2 text-xs text-secondary">Dashed lines at 70% and 90%</p>
  {/if}
{:else if items.length}
  <Panel title="Subscription limits">
    <ul class="m-0 grid list-none gap-0 p-0">
      {#each items as item (item.providerId)}
        <li class="grid items-center gap-3 border-t border-separator/70 py-3 first:border-t-0 md:grid-cols-[minmax(140px,1fr)_3fr]">
          <div class="min-w-0"><strong class="block truncate text-sm font-semibold">{item.name}</strong>{#if item.plan}<span class="text-xs text-secondary">{item.plan}</span>{/if}</div>
          <div class="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
            {#each item.windows as window (window.key)}
              {@const status = level(window.current)}
              <div class="min-w-0">
                <div class="flex items-baseline justify-between gap-2 text-xs"><span class="truncate text-secondary">{window.label}</span><b class={['text-sm font-semibold tabular-nums', status.textClass]}>{window.current == null ? '—' : `${Math.round(window.current)}%`}</b></div>
                <div class="relative mt-1 h-1.5 rounded-full bg-separator/60" role="meter" aria-valuemin={0} aria-valuemax={100} aria-valuenow={window.current ?? undefined} aria-label={`${item.name} ${window.label}`}>
                  <span class={['absolute inset-y-0 left-0 rounded-full', status.fillClass]} style:width={`${Math.min(100, window.current ?? 0)}%`}></span>
                </div>
                <div class="mt-1 text-xs text-secondary tabular-nums">{resets(window.resetsAt)}</div>
              </div>
            {/each}
          </div>
        </li>
      {/each}
    </ul>
  </Panel>
{:else if load.status === 'error'}
  <p class="text-xs text-secondary" role="alert">{load.message}</p>
{/if}
