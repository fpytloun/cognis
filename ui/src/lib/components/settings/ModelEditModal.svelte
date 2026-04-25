<script lang="ts">
  import Button from '$lib/components/ui/Button.svelte';
  import Input from '$lib/components/ui/Input.svelte';
  import BlockingDialog from '$lib/components/ui/BlockingDialog.svelte';
  import type { ModelEntry } from '$lib/types/api';

  let { model, onclose, onsave } = $props<{
    model: ModelEntry;
    onclose: () => void;
    onsave: (updated: ModelEntry) => void;
  }>();

  // Deep clone model so edits don't affect the original until save.
  // Ensure optional string/number fields have safe defaults for binding
  // (Svelte 5 $bindable props reject undefined).
  let draft: ModelEntry = $state({
    ...JSON.parse(JSON.stringify(model)),
    display_name: model.display_name ?? '',
  });

  // Cost fields need separate state since they're optional (number | undefined)
  // but Input bind:value rejects undefined.
  let inputCost = $state(String(model.input_cost_per_mtok ?? ''));
  let outputCost = $state(String(model.output_cost_per_mtok ?? ''));

  const tiers = ['nano', 'mini', 'standard', 'premium'];

  const capabilities: { key: keyof ModelEntry; label: string }[] = [
    { key: 'supports_tools', label: 'Tools' },
    { key: 'supports_vision', label: 'Vision' },
    { key: 'supports_streaming', label: 'Streaming' },
    { key: 'supports_reasoning', label: 'Reasoning' },
    { key: 'supports_audio_input', label: 'Audio input' },
    { key: 'supports_pdf_input', label: 'PDF input' },
    { key: 'supports_file_input', label: 'File input' },
    { key: 'supports_extended_thinking', label: 'Ext. thinking' },
    { key: 'supports_prompt_caching', label: 'Prompt caching' },
    { key: 'supports_responses_api', label: 'Responses API' },
    { key: 'supports_tool_search', label: 'Tool search' },
    { key: 'supports_defer_loading', label: 'Defer loading' },
    { key: 'supports_openai_namespace_tools', label: 'Namespace tools' },
    { key: 'supports_openai_allowed_tools', label: 'Allowed tools' },
    { key: 'supports_image_generation', label: 'Image generation' }
  ];

  function handleSave(): void {
    const result = { ...draft };
    const ic = parseFloat(inputCost);
    const oc = parseFloat(outputCost);
    result.input_cost_per_mtok = Number.isFinite(ic) ? ic : undefined;
    result.output_cost_per_mtok = Number.isFinite(oc) ? oc : undefined;
    onsave(result);
  }

  function toggleCapability(key: string): void {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const obj = draft as any;
    obj[key] = !obj[key];
  }
</script>

<BlockingDialog label="Edit model" onClose={onclose} titleId="model-edit-title">
  {#snippet header()}
    <div class="flex items-center justify-between gap-3">
      <h2 class="text-lg font-semibold text-white" id="model-edit-title">Edit model: {model.model_id}</h2>
      <Button aria-label="Close model editor" size="icon" variant="secondary" onclick={onclose}>&times;</Button>
    </div>
  {/snippet}

  {#snippet children()}
    <div class="space-y-5 pr-1">
      <!-- Display name -->
      <div class="space-y-1">
        <label for="model-display-name" class="text-sm font-medium text-slate-200">Display name</label>
        <Input id="model-display-name" bind:value={draft.display_name} placeholder={draft.model_id} />
        <p class="text-xs text-slate-400">Optional friendly name shown in the UI</p>
      </div>

      <!-- Limits section -->
      <div class="space-y-3">
        <h3 class="text-xs font-medium uppercase tracking-[0.25em] text-slate-400">Limits</h3>
        <div class="grid grid-cols-2 gap-4">
          <div class="space-y-1">
            <label for="model-context" class="text-sm font-medium text-slate-200">Context window</label>
            <Input id="model-context" type="number" bind:value={draft.context_window} />
          </div>
          <div class="space-y-1">
            <label for="model-output" class="text-sm font-medium text-slate-200">Max output</label>
            <Input id="model-output" type="number" bind:value={draft.max_output_tokens} />
          </div>
        </div>
        <div class="space-y-1">
          <label for="model-tier" class="text-sm font-medium text-slate-200">Tier</label>
          <select
            id="model-tier"
            bind:value={draft.tier}
            class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100"
          >
            {#each tiers as tier}
              <option value={tier}>{tier}</option>
            {/each}
          </select>
        </div>
      </div>

      <!-- Capabilities section -->
      <div class="space-y-3">
        <h3 class="text-xs font-medium uppercase tracking-[0.25em] text-slate-400">Capabilities</h3>
        <div class="grid grid-cols-2 gap-x-4 gap-y-2">
          {#each capabilities as cap}
            <label class="flex items-center gap-2 text-sm text-slate-200">
              <input
                type="checkbox"
                checked={draft[cap.key] === true}
                onchange={() => toggleCapability(cap.key)}
                class="rounded border-slate-600 bg-slate-950 text-sky-400 focus:ring-sky-300"
              />
              {cap.label}
            </label>
          {/each}
        </div>
      </div>

      <!-- Pricing section -->
      <div class="space-y-3">
        <h3 class="text-xs font-medium uppercase tracking-[0.25em] text-slate-400">Pricing (per 1M tokens)</h3>
        <div class="grid grid-cols-2 gap-4">
          <div class="space-y-1">
            <label for="model-input-cost" class="text-sm font-medium text-slate-200">Input</label>
            <Input id="model-input-cost" bind:value={inputCost} placeholder="0.00" />
          </div>
          <div class="space-y-1">
            <label for="model-output-cost" class="text-sm font-medium text-slate-200">Output</label>
            <Input id="model-output-cost" bind:value={outputCost} placeholder="0.00" />
          </div>
        </div>
      </div>
    </div>
  {/snippet}

  {#snippet footer()}
    <div class="flex justify-end gap-3">
      <Button variant="secondary" onclick={onclose}>Cancel</Button>
      <Button onclick={handleSave}>Save</Button>
    </div>
  {/snippet}
</BlockingDialog>
