<script lang="ts">
  import Button from '$lib/components/ui/Button.svelte';
  import Input from '$lib/components/ui/Input.svelte';
  import { formatBytes } from '$lib/executors';
  import {
    contextPresets,
    formatContext,
    friendlyFitStatus,
    planZone,
    statusZone
  } from '$lib/local-models';
  import type { LocalModelCatalogItem, LocalModelFitPlan } from '$lib/types/api';

  let {
    model,
    plan,
    contextTokens = $bindable(32768),
    busy = false,
    onplan,
    oncontextchange = () => {}
  } = $props<{
    model: LocalModelCatalogItem;
    plan: LocalModelFitPlan | null;
    contextTokens?: number;
    busy?: boolean;
    onplan: () => void;
    oncontextchange?: () => void;
  }>();

  const presets = $derived(contextPresets(model.advertised_max_context));
  const presetIndex = $derived(
    Math.max(0, presets.findIndex((value) => value === contextTokens))
  );
  const zone = $derived(planZone(plan));
  const zoneClasses = $derived(
    zone === 'green'
      ? 'border-emerald-500/40 bg-emerald-500/10 text-emerald-100'
      : zone === 'yellow'
        ? 'border-amber-500/40 bg-amber-500/10 text-amber-100'
        : zone === 'red'
          ? 'border-rose-500/40 bg-rose-500/10 text-rose-100'
          : 'border-slate-700 bg-slate-900 text-slate-300'
  );

  function choosePreset(event: Event): void {
    const index = Number((event.currentTarget as HTMLInputElement).value);
    const next = presets[index] ?? contextTokens;
    if (next !== contextTokens) {
      contextTokens = next;
      oncontextchange();
    }
  }

  function updateCustom(event: Event): void {
    const value = Number((event.currentTarget as HTMLInputElement).value);
    if (Number.isInteger(value) && value > 0 && value !== contextTokens) {
      contextTokens = value;
      oncontextchange();
    }
  }
</script>

<section class="space-y-4" aria-labelledby="capacity-planner-title">
  <div class="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
    <div>
      <h3 id="capacity-planner-title" class="text-lg font-semibold text-white">Choose context capacity</h3>
      <p class="mt-1 text-sm text-slate-400">This estimate is advisory. Your exact model and quantization stay unchanged.</p>
    </div>
    <div class={`rounded-xl border px-3 py-2 text-sm ${zoneClasses}`} role="status" aria-live="polite">
      {#if !plan}
        Run estimate
      {:else if zone === 'green'}
        Fits selected machines
      {:else if zone === 'yellow'}
        Likely slower with offload
      {:else if zone === 'red'}
        Capacity warning
      {:else}
        Capacity unknown
      {/if}
    </div>
  </div>

  <div class="rounded-2xl border border-slate-800 bg-slate-950/50 p-4">
    <div class="mb-3 flex items-baseline justify-between">
      <label for="context-slider" class="text-sm font-medium text-slate-200">Context window</label>
      <strong class="text-lg text-white">{formatContext(contextTokens)} tokens</strong>
    </div>
    <input
      id="context-slider"
      type="range"
      min="0"
      max={Math.max(0, presets.length - 1)}
      step="1"
      value={presetIndex}
      oninput={choosePreset}
      class="h-2 w-full cursor-pointer accent-sky-400"
      aria-valuetext={`${contextTokens} tokens`}
    />
    <div class="mt-2 flex justify-between text-[10px] text-slate-500" aria-hidden="true">
      {#each presets as preset}
        <span class={preset === contextTokens ? 'text-sky-300' : ''}>{formatContext(preset)}</span>
      {/each}
    </div>
    <div class="mt-4 grid gap-3 sm:grid-cols-[1fr_auto] sm:items-end">
      <label class="text-xs font-medium text-slate-300">
        Custom context (any positive value)
        <Input
          type="number"
          min="1"
          value={contextTokens}
          oninput={updateCustom}
          aria-label="Custom context tokens"
          class="mt-1"
        />
      </label>
      <Button onclick={onplan} disabled={busy}>{busy ? 'Estimating…' : 'Estimate fit'}</Button>
    </div>
    {#if model.advertised_max_context && contextTokens > model.advertised_max_context}
      <p class="mt-3 rounded-xl border border-rose-500/30 bg-rose-500/10 p-3 text-xs text-rose-100">
        This is above the advertised {formatContext(model.advertised_max_context)} context. Cognis will not clamp it, but deployment requires explicit acknowledgement.
      </p>
    {/if}
  </div>

  {#if plan}
    {#if plan.recommended_context_tokens}
      <p class="text-sm text-slate-300">
        Recommended common safe context:
        <strong class="text-emerald-300">{formatContext(plan.recommended_context_tokens)}</strong>
        <span class="text-slate-500">(capped at 128k)</span>
      </p>
    {:else}
      <p class="text-sm text-amber-200">No common green preset is available for every selected executor.</p>
    {/if}

    <div class="grid gap-3 lg:grid-cols-2">
      {#each plan.executors as result}
        {@const resultZone = statusZone(result.admission.status)}
        <article class={`rounded-2xl border p-4 ${resultZone === 'green' ? 'border-emerald-500/30 bg-emerald-500/5' : resultZone === 'yellow' ? 'border-amber-500/30 bg-amber-500/5' : resultZone === 'red' ? 'border-rose-500/30 bg-rose-500/5' : 'border-slate-700 bg-slate-900/60'}`}>
          <div class="flex items-start justify-between gap-3">
            <div>
              <h4 class="font-medium text-white">{result.executor_name}</h4>
              <p class="mt-1 text-sm text-slate-300">{friendlyFitStatus(result.admission.status)}</p>
            </div>
            <span class="rounded-full border border-current/20 px-2 py-1 text-[10px] uppercase tracking-wider text-slate-300">{result.admission.confidence} confidence</span>
          </div>
          <p class="mt-3 text-xs text-slate-400">
            {result.admission.reason_codes.join(' · ').replaceAll('_', ' ')}
            {result.snapshot_age_seconds != null ? ` · snapshot ${result.snapshot_age_seconds}s old` : ''}
          </p>
          <details class="mt-3 rounded-xl border border-slate-800 bg-slate-950/50 p-3">
            <summary class="cursor-pointer text-xs font-medium text-slate-300">Technical estimate</summary>
            <dl class="mt-3 grid grid-cols-2 gap-2 text-xs">
              <div><dt class="text-slate-500">Weights</dt><dd class="text-slate-200">{formatBytes(result.breakdown.weights_bytes)}</dd></div>
              <div><dt class="text-slate-500">KV cache range</dt><dd class="text-slate-200">{formatBytes(result.breakdown.kv_cache_min_bytes)}–{formatBytes(result.breakdown.kv_cache_max_bytes)}</dd></div>
              <div><dt class="text-slate-500">Runtime buffer</dt><dd class="text-slate-200">{formatBytes(result.breakdown.runtime_buffer_bytes)}</dd></div>
              <div><dt class="text-slate-500">OS headroom</dt><dd class="text-slate-200">{formatBytes(result.breakdown.reserved_headroom_bytes)}</dd></div>
              <div><dt class="text-slate-500">Estimated total</dt><dd class="text-slate-200">{formatBytes(result.breakdown.required_max_bytes)}</dd></div>
              <div><dt class="text-slate-500">{result.unified_memory ? 'Unified available' : 'Combined available'}</dt><dd class="text-slate-200">{formatBytes(result.admission.available_bytes)}</dd></div>
            </dl>
            {#if result.assumptions.length}
              <ul class="mt-3 list-disc space-y-1 pl-4 text-xs text-slate-400">
                {#each result.assumptions as assumption}<li>{assumption}</li>{/each}
              </ul>
            {/if}
          </details>
        </article>
      {/each}
    </div>
  {/if}
</section>
