<script lang="ts">
  import Button from '$lib/components/ui/Button.svelte';
  import type { ContextUsage, GenerationPerformanceSnapshot } from '$lib/types/api';

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
    last_generation?: GenerationPerformanceSnapshot | null;
  }

  let {
    detail,
    sessionId = detail.session_id ?? detail.intaris_session_id,
    contextUsage = detail.context_usage,
    performance = detail.last_generation,
    onOpenIntaris,
  } = $props<{
    detail: SessionDetailsData;
    sessionId?: string;
    contextUsage?: ContextUsage | null;
    performance?: GenerationPerformanceSnapshot | null;
    onOpenIntaris?: ((intarisSessionId: string) => void | Promise<void>) | undefined;
  }>();

  function fmt(value: number | null | undefined): string {
    return typeof value === 'number' ? value.toLocaleString() : 'Unknown';
  }

  function performanceRows(value: GenerationPerformanceSnapshot | null | undefined): Array<[string, string]> {
    if (!value) return [];
    return [
      ['Runtime', value.runtime ?? 'Unknown'],
      ['Executor', value.executor_name ?? value.executor_id ?? 'Unknown'],
      ['Latency', typeof value.total_duration_seconds === 'number' ? `${value.total_duration_seconds.toFixed(2)} s` : 'Unknown'],
      ['Tokens/s', typeof value.generation_tokens_per_second === 'number' ? value.generation_tokens_per_second.toFixed(1) : 'Unknown'],
    ];
  }
</script>

<div data-testid="session-details-content">
  <div class="flex flex-wrap items-center justify-between gap-3">
    <div>
      <p class="text-[10px] font-semibold uppercase tracking-widest text-slate-500">Session details</p>
      <p class="mt-1 break-all font-mono text-xs text-slate-300">{sessionId}</p>
    </div>
    {#if onOpenIntaris}
      <Button size="sm" variant="secondary" onclick={() => onOpenIntaris?.(detail.intaris_session_id)}>Open in Intaris</Button>
    {/if}
  </div>
  {#if detail.summary || detail.intention}
    <div class="mt-3">
      <p class="text-[10px] font-semibold uppercase tracking-widest text-slate-500">{detail.summary ? 'Summary' : 'Intention'}</p>
      <p class="mt-1 whitespace-pre-wrap text-sm leading-6 text-slate-200">{detail.summary ?? detail.intention}</p>
    </div>
  {/if}
  <div class="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-xs text-slate-400">
    <span>Status: <span class="text-slate-200">{detail.status}</span></span>
    <span>Calls: <span class="text-slate-200">{detail.total_calls}</span></span>
    <span class="text-emerald-400">{detail.approved_count} approved</span>
    <span class="text-rose-400">{detail.denied_count} denied</span>
    <span class="text-sky-400">{detail.escalated_count} escalated</span>
  </div>
  <div class="mt-4 grid gap-3 sm:grid-cols-2">
    <section class="rounded-2xl border border-slate-800 bg-slate-950/50 p-3">
      <p class="text-[10px] font-semibold uppercase tracking-widest text-slate-500">Model and context</p>
      <dl class="mt-2 grid gap-2 text-xs">
        <div><dt class="text-slate-500">Model / provider</dt><dd class="mt-0.5 text-slate-200">{performance?.model ?? contextUsage?.model ?? 'Unknown'} · {performance?.provider_name ?? performance?.provider_id ?? contextUsage?.provider_id ?? 'default'}</dd></div>
        <div><dt class="text-slate-500">Agent profile</dt><dd class="mt-0.5 text-slate-200">{contextUsage?.agent_profile_id ?? 'Default'}</dd></div>
        <div><dt class="text-slate-500">Context usage</dt><dd class="mt-0.5 text-slate-200">{fmt(contextUsage?.prompt_tokens)} / {fmt(contextUsage?.max_context_tokens)} tokens</dd></div>
        <div><dt class="text-slate-500">Projection budget</dt><dd class="mt-0.5 text-slate-200">{fmt(contextUsage?.effective_prompt_budget)} tokens</dd></div>
      </dl>
    </section>
    <section class="rounded-2xl border border-slate-800 bg-slate-950/50 p-3">
      <p class="text-[10px] font-semibold uppercase tracking-widest text-slate-500">Runtime and performance</p>
      <dl class="mt-2 grid gap-2 text-xs">
        {#each performanceRows(performance) as row}
          <div><dt class="text-slate-500">{row[0]}</dt><dd class="mt-0.5 text-slate-200">{row[1]}</dd></div>
        {/each}
        {#if !performance}<p class="text-slate-500">Runtime telemetry unavailable.</p>{/if}
      </dl>
    </section>
  </div>
</div>
