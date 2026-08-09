<script lang="ts">
  import { onMount } from 'svelte';
  import { api } from '$lib/api/client';
  import Button from '$lib/components/ui/Button.svelte';
  import type { CodexUsage, CodexUsageWindow, ContextUsage, GenerationPerformanceSnapshot, TokenUsage } from '$lib/types/api';
  import ChevronDown from 'lucide-svelte/icons/chevron-down';
  import Star from 'lucide-svelte/icons/star';

  export interface SessionDetailsData {
    session_id?: string;
    intaris_session_id: string;
    intention: string | null;
    summary: string | null;
    status: string;
    total_calls: number;
    approved_count: number;
    denied_count: number;
    escalated_count: number;
    context_usage?: ContextUsage | null;
    token_usage?: TokenUsage | null;
    last_generation?: GenerationPerformanceSnapshot | null;
  }

  let {
    detail,
    sessionId = detail.session_id ?? detail.intaris_session_id,
    contextUsage = detail.context_usage,
    tokenUsage = detail.token_usage ?? detail.context_usage?.last_llm_usage,
    performance = detail.last_generation,
    onOpenIntaris,
    canStar = false,
    starred = false,
    starBusy = false,
    onToggleStar,
  } = $props<{
    detail: SessionDetailsData;
    sessionId?: string;
    contextUsage?: ContextUsage | null;
    tokenUsage?: TokenUsage | null;
    performance?: GenerationPerformanceSnapshot | null;
    onOpenIntaris?: ((intarisSessionId: string) => void | Promise<void>) | undefined;
    canStar?: boolean;
    starred?: boolean;
    starBusy?: boolean;
    onToggleStar?: (() => void) | undefined;
  }>();
  let codexUsage = $state<CodexUsage | null>(null);
  let codexUsageError = $state<string | null>(null);

  onMount(async () => {
    const providerId = contextUsage?.provider_id;
    if (providerId !== 'codex') return;
    try {
      codexUsage = await api.llmProviders.codexUsage(providerId);
    } catch (error) {
      codexUsageError = error instanceof Error ? error.message : 'Subscription limits unavailable.';
    }
  });

  function fmt(value: number | null | undefined): string {
    return typeof value === 'number' ? value.toLocaleString() : 'Unknown';
  }

  function clampPercent(value: number | null | undefined): number {
    if (typeof value !== 'number' || !Number.isFinite(value)) return 0;
    return Math.min(100, Math.max(0, value));
  }

  function contextUsagePercent(usage: ContextUsage): number {
    if (usage.max_context_tokens <= 0) return 0;
    return clampPercent((usage.prompt_tokens / usage.max_context_tokens) * 100);
  }

  function contextBarColor(percent: number): string {
    if (percent > 95) return 'bg-rose-400';
    if (percent > 85) return 'bg-orange-400';
    if (percent > 65) return 'bg-amber-300';
    return 'bg-emerald-400';
  }

  function codexLimitLabel(window: CodexUsageWindow, position: string): string {
    const minutes = window.window_duration_mins ?? 0;
    if (minutes >= 7 * 24 * 60) return 'Weekly rolling limit';
    if (minutes >= 24 * 60) return 'Long-term rolling limit';
    return position === 'primary' ? 'Short-term rolling limit' : 'Secondary rolling limit';
  }

  function codexReset(window: CodexUsageWindow): string {
    return window.resets_at ? `Resets ${new Date(window.resets_at).toLocaleString()}` : 'Reset time unavailable';
  }

  function codexUsageWindows(usage: CodexUsage): Array<[string, CodexUsageWindow | null]> {
    return [['primary', usage.primary], ['secondary', usage.secondary]];
  }

  function contextBudgetBars(usage: ContextUsage): Array<{ label: string; value: string; percent: number }> {
    const policy = usage.projection_policy;
    const budget = usage.available_prompt_tokens ?? usage.effective_prompt_budget;
    const targets: Array<[string, number | null | undefined]> = [
      ['Effective prompt budget', budget],
      ['Loop pressure', usage.loop_pressure_threshold],
      ['Steady target', policy?.steady_target_tokens],
      ['Within-turn burst', policy?.burst_target_tokens],
      ['Hard target', policy?.hard_prompt_tokens],
    ];
    return targets.map(([label, target]) => ({
      label,
      value: `${fmt(usage.prompt_tokens)} / ${fmt(target)}`,
      percent: typeof target === 'number' && target > 0
        ? clampPercent((usage.prompt_tokens / target) * 100)
        : 0,
    }));
  }

  function tokenUsageBars(usage: TokenUsage): Array<{ label: string; value: number; percent: number; color: string }> {
    const input = Math.max(0, usage.prompt_tokens);
    const output = Math.max(0, usage.completion_tokens);
    const total = Math.max(input + output, usage.total_tokens);
    return [
      { label: 'Input', value: input, percent: total > 0 ? (input / total) * 100 : 0, color: 'bg-sky-400' },
      { label: 'Output', value: output, percent: total > 0 ? (output / total) * 100 : 0, color: 'bg-violet-400' },
    ];
  }

  function performanceRows(value: GenerationPerformanceSnapshot | null | undefined): Array<[string, string]> {
    if (!value) return [];
    const rows: Array<[string, string]> = [];
    if (value.is_local) {
      rows.push(
        ['Runtime', value.runtime ?? 'Unknown'],
        ['Executor', value.executor_name ?? value.executor_id ?? 'Unknown'],
      );
    }
    rows.push(
      ['Latency', typeof value.total_duration_seconds === 'number' ? `${value.total_duration_seconds.toFixed(2)} s` : 'Unknown'],
      ['First token', typeof value.time_to_first_token_seconds === 'number' ? `${value.time_to_first_token_seconds.toFixed(2)} s` : 'Unknown'],
      [value.is_local ? 'Tokens/s' : 'Output rate', typeof value.generation_tokens_per_second === 'number' ? value.generation_tokens_per_second.toFixed(1) : 'Unknown'],
    );
    return rows;
  }
</script>

<div data-testid="session-details-content">
  <div class="flex flex-wrap items-center justify-between gap-3">
    <div>
      <p class="text-[10px] font-semibold uppercase tracking-widest text-slate-500">Session details</p>
      <p class="mt-1 break-all font-mono text-xs text-slate-300">{sessionId}</p>
    </div>
    <div class="flex shrink-0 items-center gap-1.5">
      {#if canStar && onToggleStar}
        <button
          class={`inline-flex h-8 w-8 items-center justify-center rounded-lg border border-slate-700 transition hover:bg-slate-800 ${starred ? 'text-amber-300 hover:text-amber-200' : 'text-slate-400 hover:text-slate-100'}`}
          type="button"
          title={starred ? 'Unstar conversation' : 'Star conversation'}
          aria-label={starred ? 'Unstar conversation' : 'Star conversation'}
          disabled={starBusy}
          onclick={() => onToggleStar?.()}
          data-testid="conversation-info-star"
        >
          <Star class={`h-4 w-4 ${starred ? 'fill-current' : ''}`} />
        </button>
      {/if}
      {#if onOpenIntaris}
        <Button size="sm" variant="secondary" onclick={() => onOpenIntaris?.(detail.intaris_session_id)}>Open in Intaris</Button>
      {/if}
    </div>
  </div>
  {#if detail.summary || detail.intention}
    {@const narrative = detail.summary ?? detail.intention ?? ''}
    <details class="group mt-3" data-testid="session-narrative">
      <summary class="flex min-w-0 cursor-pointer list-none items-center gap-2 text-left">
        <span class="shrink-0 text-[10px] font-semibold uppercase tracking-widest text-slate-500">
          {detail.summary ? 'Summary' : 'Intention'}
        </span>
        <span class="min-w-0 flex-1 truncate text-xs text-slate-400 group-open:hidden">{narrative}</span>
        <ChevronDown class="h-3.5 w-3.5 shrink-0 text-slate-500 transition group-open:rotate-180" />
      </summary>
      <p class="mt-2 whitespace-pre-wrap text-sm leading-6 text-slate-200">{narrative}</p>
    </details>
  {/if}
  <div class="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-xs text-slate-400">
    <span>Status: <span class="text-slate-200">{detail.status}</span></span>
    <span>Calls: <span class="text-slate-200">{detail.total_calls}</span></span>
    <span class="text-emerald-400">{detail.approved_count} approved</span>
    <span class="text-rose-400">{detail.denied_count} denied</span>
    <span class="text-sky-400">{detail.escalated_count} escalated</span>
  </div>
  <div class="mt-4 grid gap-3 lg:grid-cols-2">
  <section class="rounded-2xl border border-slate-800 bg-slate-950/50 p-3">
    <p class="text-[10px] font-semibold uppercase tracking-widest text-slate-500">Context window</p>
    {#if contextUsage}
      {@const usagePercent = contextUsagePercent(contextUsage)}
      <div class="mt-2">
        <div class="mb-1.5 flex items-center justify-between gap-3 text-xs">
          <span class="text-slate-300">Prompt usage</span>
          <span class="text-slate-200">{fmt(contextUsage.prompt_tokens)} / {fmt(contextUsage.max_context_tokens)} <span class="text-slate-500">({usagePercent.toFixed(0)}%)</span></span>
        </div>
        <div
          aria-label={`Context usage: ${usagePercent.toFixed(0)}%`}
          aria-valuemax="100"
          aria-valuemin="0"
          aria-valuenow={usagePercent}
          class="h-2 overflow-hidden rounded-full bg-slate-800"
          data-testid="session-context-usage-bar"
          role="progressbar"
        >
          <div class={`h-full rounded-full ${contextBarColor(usagePercent)}`} style={`width: ${usagePercent}%`}></div>
        </div>
        <p class="mt-2 text-xs text-slate-500">
          {performance?.model ?? contextUsage.model}
          {contextUsage.agent_profile_id && contextUsage.agent_profile_id !== 'default' ? ` · profile ${contextUsage.agent_profile_id}` : ''}
        </p>
      </div>
    {:else}
      <p class="mt-2 text-xs text-slate-500">Context usage unavailable.</p>
    {/if}

    {#if tokenUsage}
      <div class="mt-3 border-t border-slate-800 pt-3" data-testid="session-token-usage">
        <div class="mb-2 flex items-center justify-between gap-3 text-xs">
          <p class="font-semibold uppercase tracking-widest text-slate-500">Last call usage</p>
          <span class="text-slate-300">{fmt(tokenUsage.total_tokens)} tokens</span>
        </div>
        <div class="flex h-2 overflow-hidden rounded-full bg-slate-800" aria-label={`Last call token usage: ${fmt(tokenUsage.prompt_tokens)} input and ${fmt(tokenUsage.completion_tokens)} output tokens`}>
          {#each tokenUsageBars(tokenUsage) as token}
            {#if token.percent > 0}
              <div class={token.color} style={`width: ${token.percent}%`}></div>
            {/if}
          {/each}
        </div>
        <div class="mt-1.5 flex gap-4 text-xs">
          <span class="text-sky-300">Input {fmt(tokenUsage.prompt_tokens)}</span>
          <span class="text-violet-300">Output {fmt(tokenUsage.completion_tokens)}</span>
          {#if tokenUsage.cache_read_input_tokens}
            <span class="text-emerald-300">Cache read {fmt(tokenUsage.cache_read_input_tokens)}</span>
          {/if}
          {#if tokenUsage.cache_write_tokens ?? tokenUsage.cache_creation_input_tokens}
            <span class="text-amber-300">Cache write {fmt(tokenUsage.cache_write_tokens ?? tokenUsage.cache_creation_input_tokens)}</span>
          {/if}
        </div>
      </div>
    {/if}
  </section>

  <section class="rounded-2xl border border-slate-800 bg-slate-950/50 p-3">
    {#if contextUsage?.provider_id === 'codex'}
      <p class="text-[10px] font-semibold uppercase tracking-widest text-slate-500">Codex subscription limits</p>
      {#if codexUsage}
        <p class="mt-2 text-xs text-slate-400">Plan <span class="text-slate-200">{codexUsage.plan_type ?? 'unknown'}</span>{codexUsage.fetched_at ? ` · fetched ${new Date(codexUsage.fetched_at).toLocaleString()}` : ''}</p>
        <div class="mt-3 space-y-3" data-testid="session-codex-subscription-limits">
          {#each codexUsageWindows(codexUsage) as [position, window]}
            {#if window}
              <div>
                <div class="mb-1.5 flex items-center justify-between gap-3 text-xs">
                  <span class="text-slate-300">{codexLimitLabel(window, position)}</span>
                  <span class="text-slate-200">{Math.round(window.used_percent)}% used</span>
                </div>
                <div class="h-2 overflow-hidden rounded-full bg-slate-800" role="progressbar" aria-label={`Codex ${position} subscription limit`} aria-valuemin="0" aria-valuemax="100" aria-valuenow={window.used_percent}>
                  <div class={`h-full rounded-full ${contextBarColor(window.used_percent)}`} style={`width: ${clampPercent(window.used_percent)}%`}></div>
                </div>
                <p class="mt-1 text-xs text-slate-500">{codexReset(window)}</p>
              </div>
            {/if}
          {/each}
          {#each codexUsage.additional_rate_limits as limit}
            {#if limit.primary}
              <div>
                <div class="mb-1.5 flex items-center justify-between gap-3 text-xs">
                  <span class="text-slate-300">{limit.limit_name ?? limit.limit_id ?? 'Additional feature limit'}</span>
                  <span class="text-slate-200">{Math.round(limit.primary.used_percent)}% used</span>
                </div>
                <div class="h-2 overflow-hidden rounded-full bg-slate-800" role="progressbar" aria-label={`${limit.limit_name ?? limit.limit_id ?? 'Additional'} subscription limit`} aria-valuemin="0" aria-valuemax="100" aria-valuenow={limit.primary.used_percent}>
                  <div class={`h-full rounded-full ${contextBarColor(limit.primary.used_percent)}`} style={`width: ${clampPercent(limit.primary.used_percent)}%`}></div>
                </div>
                <p class="mt-1 text-xs text-slate-500">{codexReset(limit.primary)}</p>
              </div>
            {/if}
          {/each}
        </div>
      {:else if codexUsageError}
        <p class="mt-2 text-xs text-slate-500">Subscription limits unavailable: {codexUsageError}</p>
      {:else}
        <p class="mt-2 text-xs text-slate-500">Loading authenticated subscription limits…</p>
      {/if}
    {:else if performance}
      <p class="text-[10px] font-semibold uppercase tracking-widest text-slate-500">{performance.is_local ? 'Runtime and performance' : 'Request performance'}</p>
      <dl class="mt-3 grid gap-2 text-xs">
        {#each performanceRows(performance) as row}
          <div><dt class="text-slate-500">{row[0]}</dt><dd class="mt-0.5 text-slate-200">{row[1]}</dd></div>
        {/each}
      </dl>
    {:else}
      <p class="text-[10px] font-semibold uppercase tracking-widest text-slate-500">Provider usage</p>
      <p class="mt-2 text-xs text-slate-500">Usage appears after the provider reports a completed call.</p>
    {/if}
  </section>
  </div>

  <section class="mt-3 rounded-2xl border border-slate-800 bg-slate-950/50 p-3">
    <details class="group" data-testid="session-details-diagnostics">
      <summary class="cursor-pointer list-none text-xs font-medium text-sky-300 hover:text-sky-200">
        <span class="group-open:hidden">Show context &amp; runtime details</span>
        <span class="hidden group-open:inline">Hide context &amp; runtime details</span>
      </summary>
      <div class="mt-3 grid gap-3 sm:grid-cols-2">
        <div class="space-y-2">
          {#if contextUsage}
            {#each contextBudgetBars(contextUsage) as metric}
              <div class="rounded-xl border border-slate-800/70 bg-slate-950/40 px-2.5 py-2">
                <div class="mb-1.5 flex items-center justify-between gap-3 text-xs">
                  <span class="text-slate-400">{metric.label}</span>
                  <span class="text-slate-200">{metric.value}</span>
                </div>
                <div class="h-1.5 overflow-hidden rounded-full bg-slate-800">
                  <div class={`h-full rounded-full ${contextBarColor(metric.percent)}`} style={`width: ${metric.percent}%`}></div>
                </div>
              </div>
            {/each}
          {/if}
        </div>
        <dl class="grid content-start gap-2 text-xs">
          <div><dt class="text-slate-500">Model / provider</dt><dd class="mt-0.5 text-slate-200">{performance?.model ?? contextUsage?.model ?? 'Unknown'} · {performance?.provider_name ?? performance?.provider_id ?? contextUsage?.provider_id ?? 'default'}</dd></div>
          <div><dt class="text-slate-500">Agent profile</dt><dd class="mt-0.5 text-slate-200">{contextUsage?.agent_profile_id ?? 'Default'}</dd></div>
          <div><dt class="text-slate-500">Projection budget</dt><dd class="mt-0.5 text-slate-200">{fmt(contextUsage?.available_prompt_tokens ?? contextUsage?.effective_prompt_budget)} tokens</dd></div>
          <div><dt class="text-slate-500">Provider</dt><dd class="mt-0.5 text-slate-200">{contextUsage?.provider_id ?? 'default'}</dd></div>
        </dl>
      </div>
    </details>
  </section>
</div>
