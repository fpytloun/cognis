<script lang="ts">
  import Info from 'lucide-svelte/icons/info';
  import Plus from 'lucide-svelte/icons/plus';
  import Trash2 from 'lucide-svelte/icons/trash-2';

  import Badge from '$lib/components/ui/Badge.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import Input from '$lib/components/ui/Input.svelte';
  import Tooltip from '$lib/components/ui/Tooltip.svelte';
  import {
    parseSettingDraft,
    serializeSettingValue,
    settingControlKind,
    settingInfoText,
    settingOptionLabel,
    settingOptionPayload,
    settingOptionValue
  } from '$lib/system-settings';
  import type { Setting } from '$lib/types/api';

  let {
    setting,
    draft,
    error = '',
    busy = false,
    disabled = false,
    onchange,
    onapply,
    onreset
  } = $props<{
    setting: Setting;
    draft: string;
    error?: string;
    busy?: boolean;
    disabled?: boolean;
    onchange: (value: string) => void;
    onapply: () => void;
    onreset: () => void;
  }>();

  let kind = $derived(settingControlKind(setting));
  let dirty = $derived(draft !== serializeSettingValue(setting));
  let listItems = $derived.by(() => {
    if (kind !== 'string-list') return [];
    const parsed = parseSettingDraft(setting, draft);
    return Array.isArray(parsed.value) ? parsed.value as string[] : [];
  });

  function updateListItem(index: number, value: string): void {
    onchange(JSON.stringify(listItems.map((item, itemIndex) => itemIndex === index ? value : item)));
  }

  function addListItem(): void {
    onchange(JSON.stringify([...listItems, '']));
  }

  function removeListItem(index: number): void {
    onchange(JSON.stringify(listItems.filter((_, itemIndex) => itemIndex !== index)));
  }

  function updateEnum(event: Event): void {
    const selected = (event.currentTarget as HTMLSelectElement).value;
    const option = setting.options?.find((candidate: unknown) => settingOptionValue(candidate) === selected);
    if (option !== undefined) onchange(JSON.stringify(settingOptionPayload(option)));
  }
</script>

<div
  class={[
    'rounded-2xl border p-4 transition',
    setting.is_overridden ? 'border-amber-500/40 bg-amber-500/[0.06]' : 'border-slate-800 bg-slate-950/60',
    dirty ? 'ring-1 ring-sky-500/35' : ''
  ]}
  data-setting-key={setting.key}
>
  <div class="flex items-start justify-between gap-3">
    <div class="min-w-0">
      <div class="flex flex-wrap items-center gap-2">
        <span class="font-medium text-white">{setting.label || setting.key}</span>
        {#if setting.is_overridden}
          <Badge class="border-amber-500/40 bg-amber-500/15 text-amber-200">Customized</Badge>
        {/if}
        {#if dirty}
          <Badge class="border-sky-500/40 bg-sky-500/15 text-sky-200">Unsaved</Badge>
        {/if}
      </div>
      {#if setting.description}
        <p class="mt-1 line-clamp-2 text-sm text-slate-400">{setting.description}</p>
      {/if}
    </div>
    <Tooltip text={settingInfoText(setting)} placement="left">
      <button
        type="button"
        class="inline-flex h-11 w-11 shrink-0 items-center justify-center rounded-xl text-slate-400 transition hover:bg-slate-800 hover:text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-300 md:h-9 md:w-9"
        aria-label={`Information about ${setting.label || setting.key}`}
        aria-describedby={`setting-info-${setting.key}`}
      >
        <Info class="h-4 w-4" aria-hidden="true" />
      </button>
    </Tooltip>
    <span id={`setting-info-${setting.key}`} class="sr-only">{settingInfoText(setting)}</span>
  </div>

  <div class="mt-4">
    {#if kind === 'boolean'}
      <label class="inline-flex min-h-11 cursor-pointer items-center gap-3 text-sm text-slate-200" for={`setting-${setting.key}`}>
        <input
          id={`setting-${setting.key}`}
          type="checkbox"
          class="h-5 w-5 rounded border-slate-600 bg-slate-900 text-sky-500 focus:ring-sky-400"
          checked={draft === 'true'}
          disabled={disabled || busy}
          aria-label={setting.label || setting.key}
          onchange={(event) => onchange(event.currentTarget.checked ? 'true' : 'false')}
        />
        <span>{draft === 'true' ? 'Enabled' : 'Disabled'}</span>
      </label>
    {:else if kind === 'enum'}
      <select
        id={`setting-${setting.key}`}
        value={draft}
        disabled={disabled || busy}
        aria-label={setting.label || setting.key}
        onchange={updateEnum}
        class="min-h-11 w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 text-base text-slate-100 focus:border-sky-500 focus:outline-none focus:ring-2 focus:ring-sky-500/30 md:min-h-9 md:text-sm"
      >
        {#each setting.options ?? [] as option}
          <option value={settingOptionValue(option)}>{settingOptionLabel(option)}</option>
        {/each}
      </select>
    {:else if kind === 'number'}
      <div class="flex items-center gap-2">
        <Input
          id={`setting-${setting.key}`}
          type="number"
          value={draft}
          min={setting.minimum ?? undefined}
          max={setting.maximum ?? undefined}
          step={['integer', 'int'].includes(setting.value_type.toLowerCase()) ? 1 : 'any'}
          disabled={disabled || busy}
          aria-label={setting.label || setting.key}
          oninput={(event) => onchange(event.currentTarget.value)}
          aria-invalid={Boolean(error)}
          aria-describedby={error ? `setting-error-${setting.key}` : undefined}
        />
        {#if setting.unit}<span class="shrink-0 text-sm text-slate-400">{setting.unit}</span>{/if}
      </div>
    {:else if kind === 'string'}
      <Input
        id={`setting-${setting.key}`}
        value={draft}
        disabled={disabled || busy}
        aria-label={setting.label || setting.key}
        oninput={(event) => onchange(event.currentTarget.value)}
        aria-invalid={Boolean(error)}
        aria-describedby={error ? `setting-error-${setting.key}` : undefined}
      />
    {:else if kind === 'string-list'}
      <div id={`setting-${setting.key}`} class="space-y-2">
        {#each listItems as item, index (`${setting.key}-${index}`)}
          <div class="flex items-center gap-2">
            <Input
              value={item}
              disabled={disabled || busy}
              aria-label={`${setting.label || setting.key} item ${index + 1}`}
              oninput={(event) => updateListItem(index, event.currentTarget.value)}
            />
            <Button
              size="icon-mobile"
              variant="ghost"
              disabled={disabled || busy}
              aria-label={`Remove item ${index + 1} from ${setting.label || setting.key}`}
              onclick={() => removeListItem(index)}
            >
              <Trash2 class="h-4 w-4" aria-hidden="true" />
            </Button>
          </div>
        {/each}
        <Button size="sm" variant="secondary" disabled={disabled || busy} onclick={addListItem}>
          <Plus class="mr-1 h-4 w-4" aria-hidden="true" /> Add item
        </Button>
      </div>
    {:else}
      <textarea
        id={`setting-${setting.key}`}
        value={draft}
        disabled={disabled || busy}
        aria-label={setting.label || setting.key}
        oninput={(event) => onchange(event.currentTarget.value)}
        aria-invalid={Boolean(error)}
        aria-describedby={error ? `setting-error-${setting.key}` : undefined}
        class="min-h-36 w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2.5 font-mono text-base text-slate-100 focus:border-sky-500 focus:outline-none focus:ring-2 focus:ring-sky-500/30 md:text-sm"
      ></textarea>
    {/if}
  </div>

  {#if error}
    <p id={`setting-error-${setting.key}`} class="mt-2 text-sm text-rose-300" role="alert">{error}</p>
  {/if}

  <div class="mt-4 flex flex-wrap items-center justify-between gap-2 border-t border-slate-800/80 pt-3">
    <div class="text-xs text-slate-500">
      {#if setting.updated_by}
        Last changed by {setting.updated_by}
      {:else}
        Using {setting.is_overridden ? 'a customized value' : 'the default value'}
      {/if}
    </div>
    <div class="flex flex-wrap gap-2">
      {#if setting.is_overridden}
        <Button size="sm" variant="ghost" disabled={disabled || busy} onclick={onreset}>Reset to default</Button>
      {/if}
      <Button size="sm" disabled={disabled || busy || !dirty} onclick={onapply}>
        {busy ? 'Applying…' : 'Apply'}
      </Button>
    </div>
  </div>
</div>
