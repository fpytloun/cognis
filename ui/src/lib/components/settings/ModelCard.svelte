<script lang="ts">
  import Badge from '$lib/components/ui/Badge.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import { formatTokenCount } from '$lib/providers';
  import type { ModelEntry } from '$lib/types/api';

  let { model, isDefault = false, onedit, onremove } = $props<{
    model: ModelEntry;
    isDefault: boolean;
    onedit: () => void;
    onremove: () => void;
  }>();

  let metaLine = $derived.by(() => {
    const parts: string[] = [];
    parts.push(`${formatTokenCount(model.context_window)} ctx`);
    if (model.max_input_tokens && model.max_input_tokens !== model.context_window) {
      parts.push(`${formatTokenCount(model.max_input_tokens)} in`);
    }
    parts.push(`${formatTokenCount(model.max_output_tokens)} out`);
    if (model.input_cost_per_mtok != null && model.output_cost_per_mtok != null) {
      parts.push(`$${model.input_cost_per_mtok}/$${model.output_cost_per_mtok} per Mtok`);
    }
    return parts.join(' \u00b7 ');
  });

  function metadataString(record: Record<string, unknown> | undefined, key: string): string | null {
    const value = record?.[key];
    return typeof value === 'string' && value.trim() ? value : null;
  }

  function metadataPositiveNumber(record: Record<string, unknown> | undefined, key: string): number | null {
    const value = record?.[key];
    return typeof value === 'number' && Number.isFinite(value) && value > 0 ? value : null;
  }

  let providerMetaLine = $derived.by(() => {
    const providerMetadata = model.provider_metadata ?? {};
    if (providerMetadata.provider !== 'ollama') {
      return '';
    }
    const runtimeMetadata = model.runtime_metadata ?? {};
    const details =
      typeof providerMetadata.details === 'object' && providerMetadata.details !== null && !Array.isArray(providerMetadata.details)
        ? (providerMetadata.details as Record<string, unknown>)
        : {};
    const parts = [
      metadataString(details, 'parameter_size'),
      metadataString(details, 'quantization_level'),
      metadataString(details, 'family')
    ].filter((value): value is string => Boolean(value));
    const runtimeNumCtx = metadataPositiveNumber(runtimeMetadata, 'num_ctx');
    const maxContextWindow =
      typeof model.max_context_window === 'number' && Number.isFinite(model.max_context_window) && model.max_context_window > 0
        ? model.max_context_window
        : null;
    const contextWindow =
      typeof model.context_window === 'number' && Number.isFinite(model.context_window) && model.context_window > 0
        ? model.context_window
        : null;
    const runtimeContextWindow = runtimeNumCtx ?? (
      contextWindow !== null && maxContextWindow !== null ? Math.min(contextWindow, maxContextWindow) : contextWindow
    );
    if (runtimeContextWindow !== null) {
      parts.push(`runtime num_ctx ${formatTokenCount(runtimeContextWindow)}`);
    }
    return parts.join(' \u00b7 ');
  });

  const capabilityBadges: { key: keyof ModelEntry; label: string }[] = [
    { key: 'supports_tools', label: 'tools' },
    { key: 'supports_vision', label: 'vision' },
    { key: 'supports_streaming', label: 'streaming' },
    { key: 'supports_reasoning', label: 'reasoning' },
    { key: 'supports_embedding', label: 'embedding' },
    { key: 'supports_prompt_caching', label: 'prompt-caching' },
    { key: 'supports_responses_api', label: 'responses-api' },
    { key: 'supports_tool_search', label: 'tool-search' },
    { key: 'supports_openai_namespace_tools', label: 'namespace-tools' },
    { key: 'supports_openai_allowed_tools', label: 'allowed-tools' },
    { key: 'supports_image_generation', label: 'image-gen' }
  ];

  let activeBadges = $derived(capabilityBadges.filter((b) => model[b.key] === true));
</script>

<div class="rounded-2xl border border-slate-800 bg-slate-950/40 p-4">
  <div class="flex items-start justify-between gap-3">
    <div class="min-w-0 flex-1 space-y-1.5">
      <!-- Model ID + default star -->
      <div class="flex items-center gap-2">
        {#if isDefault}
          <span class="text-sky-400" title="Default model">&#9733;</span>
        {/if}
        <span class="text-sm font-bold text-slate-100">{model.display_name ?? model.model_id}</span>
        {#if model.display_name}
          <span class="text-xs text-slate-500">{model.model_id}</span>
        {/if}
      </div>

      <!-- Metadata line -->
      <p class="text-xs text-slate-400">{metaLine}</p>
      {#if providerMetaLine}
        <p class="text-xs text-slate-500">Ollama: {providerMetaLine}</p>
      {/if}

      <!-- Capability badges -->
      {#if activeBadges.length > 0}
        <div class="flex flex-wrap gap-1.5">
          {#each activeBadges as badge}
            <Badge class="text-[10px] px-2 py-0.5">{badge.label}</Badge>
          {/each}
        </div>
      {/if}
    </div>

    <!-- Actions -->
    <div class="flex shrink-0 items-center gap-1">
      <Button size="sm" variant="ghost" onclick={onedit}>Edit</Button>
      <Button size="sm" variant="ghost" onclick={onremove}>Remove</Button>
    </div>
  </div>
</div>
