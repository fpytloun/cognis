<script lang="ts">
  import Button from '$lib/components/ui/Button.svelte';
  import { formatBytes } from '$lib/executors';
  import { formatContext } from '$lib/local-models';
  import type { LocalModelCatalogItem, LocalModelQuantization } from '$lib/types/api';

  let {
    model,
    selected = false,
    onselect,
    ondetails
  } = $props<{
    model: LocalModelCatalogItem;
    selected?: boolean;
    onselect: (model: LocalModelCatalogItem, requestedRef: string) => void;
    ondetails?: (model: LocalModelCatalogItem) => void;
  }>();

  let requestedRef = $state('');
  let cardElement = $state<HTMLElement | null>(null);

  $effect(() => {
    if (!model.quantizations.some((item: LocalModelQuantization) => item.requested_ref === requestedRef)) {
      requestedRef = model.quantizations[0]?.requested_ref ?? model.requested_ref;
    }
  });

  $effect(() => {
    if (
      !cardElement ||
      model.source !== 'huggingface' ||
      model.metadata_status !== 'basic' ||
      !ondetails
    ) {
      return;
    }
    if (typeof IntersectionObserver === 'undefined') {
      ondetails(model);
      return;
    }
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) {
          ondetails?.(model);
          observer.disconnect();
        }
      },
      { rootMargin: '160px' }
    );
    observer.observe(cardElement);
    return () => observer.disconnect();
  });

  function formatParameters(value: number | null): string {
    if (value == null) return 'Unknown parameters';
    return `${(value / 1_000_000_000).toLocaleString(undefined, { maximumFractionDigits: 1 })}B params`;
  }
</script>

<article
  bind:this={cardElement}
  class={`flex h-full flex-col rounded-2xl border p-4 transition ${selected ? 'border-sky-400/70 bg-sky-500/10' : 'border-slate-800 bg-slate-900/65 hover:border-slate-600'}`}
  aria-label={model.title}
>
  <div class="flex items-start justify-between gap-3">
    <div class="min-w-0">
      <h3 class="truncate text-base font-semibold text-white">
        {#if model.repository_url}
          <a
            class="hover:text-sky-200"
            href={model.repository_url}
            target="_blank"
            rel="noreferrer"
            aria-label={`Open ${model.title} repository on Hugging Face (opens in a new tab)`}
          >{model.title}</a>
        {:else}
          {model.title}
        {/if}
      </h3>
      <p class="mt-1 text-xs text-slate-400">{model.publisher ?? 'Unknown publisher'} · {model.license ?? 'License not listed'}</p>
    </div>
    <span class={`shrink-0 rounded-full border px-2 py-1 text-[10px] font-semibold uppercase tracking-wider ${model.source === 'huggingface' ? 'border-amber-500/30 bg-amber-500/10 text-amber-200' : 'border-violet-500/30 bg-violet-500/10 text-violet-200'}`}>
      {model.source === 'huggingface' ? 'HF GGUF' : 'Ollama'}
    </span>
  </div>

  <p class="mt-3 line-clamp-2 min-h-10 text-sm leading-5 text-slate-300">
    {model.description ?? 'Catalog metadata is limited. Review the model source before deployment.'}
  </p>

  <div class="mt-3 flex flex-wrap gap-1.5">
    {#each model.capabilities as capability}
      <span class="rounded-md bg-slate-800 px-2 py-1 text-[11px] capitalize text-slate-300">{capability}</span>
    {/each}
  </div>

  <dl class="mt-3 grid grid-cols-2 gap-x-3 gap-y-1 text-xs">
    <div><dt class="sr-only">Parameters</dt><dd class="text-slate-400">{formatParameters(model.parameter_count)}</dd></div>
    <div><dt class="sr-only">Context</dt><dd class="text-right text-slate-400">{model.advertised_max_context ? `${formatContext(model.advertised_max_context)} context` : 'Unknown context'}</dd></div>
    {#if model.downloads != null}
      <div><dt class="sr-only">Downloads</dt><dd class="text-slate-400">{model.downloads.toLocaleString()} downloads</dd></div>
    {/if}
    {#if model.likes != null}
      <div><dt class="sr-only">Likes</dt><dd class="text-right text-slate-400">{model.likes.toLocaleString()} likes</dd></div>
    {/if}
  </dl>

  {#if model.metadata_status === 'basic' && model.source === 'huggingface'}
    <p class="mt-3 text-xs text-slate-500">Loading repository details when visible…</p>
  {:else if model.metadata_diagnostics.length}
    <p class="mt-3 text-xs text-amber-200">{model.metadata_diagnostics[0]}</p>
  {:else if model.warnings.length}
    <p class="mt-3 text-xs text-amber-200">{model.warnings[0]}</p>
  {/if}

  {#if model.model_card_url}
    <a
      class="mt-3 text-xs text-sky-300 hover:text-sky-200"
      href={model.model_card_url}
      target="_blank"
      rel="noreferrer"
      aria-label={`Open ${model.title} model card (opens in a new tab)`}
    >Model card ↗</a>
  {/if}

  <div class="mt-auto pt-4">
    <label class="text-xs font-medium text-slate-300">
      Quantization / reference
      <select
        bind:value={requestedRef}
        class="mt-1 min-h-10 w-full rounded-xl border border-slate-700 bg-slate-950 px-3 text-sm text-slate-100"
        aria-label={`${model.title} quantization`}
      >
        {#each model.quantizations as quantization}
          <option value={quantization.requested_ref}>
            {quantization.name}{quantization.size_bytes != null ? ` · ${formatBytes(quantization.size_bytes)}` : ''}
          </option>
        {/each}
      </select>
    </label>
    <Button
      class="mt-3 w-full"
      variant={selected ? 'secondary' : 'primary'}
      onclick={() => onselect(model, requestedRef)}
    >
      {selected ? 'Selected' : 'Plan deployment'}
    </Button>
  </div>
</article>
