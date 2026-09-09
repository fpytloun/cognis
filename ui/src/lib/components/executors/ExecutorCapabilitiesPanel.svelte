<script lang="ts">
  import {
    capabilityStatusLabel,
    capabilityStatusTone,
    executorCapabilityFreshness
  } from '$lib/executors';
  import type { RuntimeCapabilityStatus, ExecutorConfig } from '$lib/types/api';

  let { executor } = $props<{ executor: ExecutorConfig }>();

  const report = $derived(executor.observed_capabilities);
  const freshness = $derived(executorCapabilityFreshness(report));
  type StatusEntry = [string, RuntimeCapabilityStatus];
  const statusEntries = (value: Record<string, RuntimeCapabilityStatus>): StatusEntry[] => Object.entries(value);
  const componentEntries = $derived(report ? statusEntries(report.components) : []);

  const runtimeEntries = $derived(report ? statusEntries(report.browser.runtimes) : []);
  const engineEntries = $derived(report ? statusEntries(report.browser.engines) : []);
  const channelEntries = $derived(report ? statusEntries(report.browser.channels) : []);
  const runtimeSupportEntries = $derived<StatusEntry[]>(report ? [
    ['officecli', report.officecli],
    ['mcp_launch', report.mcp_launch],
    ['git', report.git],
    ['node', report.node],
    ['uv', report.uv],
    ['lsp', report.lsp]
  ] : []);
  const DISPLAY_NAMES: Record<string, string> = {
    mcp: 'MCP',
    mcp_launch: 'MCP launch',
    officecli: 'OfficeCLI',
    lsp: 'LSP',
    uv: 'uv'
  };

  function statusText(status: RuntimeCapabilityStatus): string {
    return status.version ? `${capabilityStatusLabel(status.state)} · v${status.version}` : capabilityStatusLabel(status.state);
  }

  function formatObservedAt(value: string): string {
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? 'Time unknown' : date.toLocaleString();
  }

  function titleCase(value: string): string {
    return DISPLAY_NAMES[value] ?? value.replace(/[_-]+/g, ' ').replace(/\b\w/g, (character) => character.toUpperCase());
  }
</script>

<section
  class="space-y-4"
  aria-label={`Runtime capabilities for ${executor.name}`}
  data-testid="executor-capabilities-panel"
>
  <div class="flex flex-col gap-3 rounded-2xl border border-slate-800 bg-slate-950/45 px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
    <div>
      <div class="flex flex-wrap items-center gap-2">
        <h3 class="text-sm font-semibold text-slate-100">Runtime capabilities</h3>
        {#if report}
          <span class="rounded-full border border-slate-700 px-2 py-0.5 text-[10px] uppercase tracking-wider text-slate-400">
            schema v{report.schema_version}
          </span>
        {/if}
      </div>
      {#if report}
        <p class="mt-1 text-xs text-slate-400">
          Executor v{report.executor_version}
          {#if report.image_variant} · {titleCase(report.image_variant)} image{/if}
        </p>
      {:else}
        <p class="mt-1 text-xs text-slate-400">No capability report is available for this executor.</p>
      {/if}
    </div>
    {#if report}
      <div class="text-left text-xs text-slate-400 sm:text-right">
        <p class={freshness.state === 'stale' ? 'text-amber-300' : ''}>{freshness.label}</p>
        <p class="mt-1 text-slate-500">{formatObservedAt(report.observed_at)}</p>
      </div>
    {/if}
  </div>

  {#if report}
    <div class="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
      <article class="rounded-2xl border border-slate-800 bg-slate-950/55 p-4">
        <h4 class="text-xs font-semibold uppercase tracking-[0.18em] text-slate-500">Browser runtimes</h4>
        <div class="mt-3 space-y-2">
          {#each runtimeEntries as [name, status]}
            <div class="flex items-center justify-between gap-3 text-sm">
              <span class="text-slate-300">{titleCase(name)}</span>
              <span class={`rounded-full border px-2 py-0.5 text-[10px] ${capabilityStatusTone(status.state)}`} title={status.message}>{statusText(status)}</span>
            </div>
          {/each}
          {#if runtimeEntries.length === 0}<p class="text-xs text-slate-500">Not reported</p>{/if}
        </div>
      </article>

      <article class="rounded-2xl border border-slate-800 bg-slate-950/55 p-4">
        <h4 class="text-xs font-semibold uppercase tracking-[0.18em] text-slate-500">Browser engines & channels</h4>
        <div class="mt-3 space-y-2">
          {#each [...engineEntries, ...channelEntries] as [name, status]}
            <div class="flex items-center justify-between gap-3 text-sm">
              <span class="text-slate-300">{titleCase(name)}</span>
              <span class={`rounded-full border px-2 py-0.5 text-[10px] ${capabilityStatusTone(status.state)}`} title={status.message}>{statusText(status)}</span>
            </div>
          {/each}
          {#if engineEntries.length === 0 && channelEntries.length === 0}<p class="text-xs text-slate-500">Not reported</p>{/if}
        </div>
      </article>

      <article class="rounded-2xl border border-slate-800 bg-slate-950/55 p-4">
        <h4 class="text-xs font-semibold uppercase tracking-[0.18em] text-slate-500">Runtime support</h4>
        <div class="mt-3 grid grid-cols-2 gap-2">
          {#each runtimeSupportEntries as [name, status]}
            <div class="rounded-xl border border-slate-800 bg-slate-900/60 p-2">
              <p class="text-xs text-slate-400">{titleCase(name)}</p>
              <p class={`mt-1 text-xs ${capabilityStatusTone(status.state).split(' ').at(-1)}`}>{statusText(status)}</p>
              <p class="mt-1 line-clamp-2 text-[11px] text-slate-500" title={status.message}>{status.message}</p>
            </div>
          {/each}
        </div>
      </article>
    </div>

    <div class="grid gap-3 lg:grid-cols-3">
      <article class="rounded-2xl border border-slate-800 bg-slate-950/55 p-4">
        <h4 class="text-xs font-semibold uppercase tracking-[0.18em] text-slate-500">Configured desired tools</h4>
        <p class="mt-2 text-sm text-slate-300">{report.desired_tools.length ? report.desired_tools.join(', ') : 'None reported'}</p>
      </article>
      <article class="rounded-2xl border border-slate-800 bg-slate-950/55 p-4">
        <h4 class="text-xs font-semibold uppercase tracking-[0.18em] text-slate-500">Active observed tools</h4>
        <p class="mt-2 text-sm text-emerald-200">{report.observed_tools.length ? report.observed_tools.join(', ') : 'None reported'}</p>
      </article>
      <article class="rounded-2xl border border-slate-800 bg-slate-950/55 p-4">
        <h4 class="text-xs font-semibold uppercase tracking-[0.18em] text-slate-500">Supported definitions</h4>
        <p class="mt-2 text-sm text-slate-300">{report.supported_tools.length ? report.supported_tools.join(', ') : 'None reported'}</p>
      </article>
    </div>

    <details class="rounded-2xl border border-slate-800 bg-slate-950/40 px-4 py-3">
      <summary class="cursor-pointer text-xs font-semibold uppercase tracking-[0.18em] text-slate-400">Component summary</summary>
      <div class="mt-3 grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
        {#each componentEntries as [name, status]}
          <div class="flex items-center justify-between gap-3 rounded-xl border border-slate-800 bg-slate-900/50 px-3 py-2">
            <span class="text-sm text-slate-300">{titleCase(name)}</span>
            <span class={`rounded-full border px-2 py-0.5 text-[10px] ${capabilityStatusTone(status.state)}`} title={status.message}>{capabilityStatusLabel(status.state)}</span>
          </div>
        {/each}
      </div>
    </details>
  {:else}
    <div class="rounded-2xl border border-dashed border-slate-700 bg-slate-950/40 px-4 py-6 text-center">
      <p class="text-sm text-slate-300">Runtime capability details are not available.</p>
      <p class="mt-1 text-xs text-slate-500">Legacy, offline, or older executors can still be configured. Connect or update the executor to report its runtime.</p>
    </div>
  {/if}
</section>
