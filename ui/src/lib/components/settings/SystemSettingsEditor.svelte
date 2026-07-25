<script lang="ts">
  import { onDestroy, onMount } from 'svelte';

  import { api, asApiError } from '$lib/api/client';
  import Card from '$lib/components/ui/Card.svelte';
  import { confirmAction } from '$lib/stores/confirm';
  import { addToast } from '$lib/stores/toasts';
  import {
    groupSystemSettings,
    parseSettingDraft,
    replaceSetting,
    serializeSettingValue
  } from '$lib/system-settings';
  import type { Setting, SettingsCategory } from '$lib/types/api';
  import SystemSettingRow from './SystemSettingRow.svelte';

  const REFRESH_INTERVAL_MS = 30_000;

  let {
    settings,
    disabled = false,
    onsettingschange,
    ondirtychange
  } = $props<{
    settings: SettingsCategory[];
    disabled?: boolean;
    onsettingschange: (settings: SettingsCategory[]) => void;
    ondirtychange?: (dirty: boolean) => void;
  }>();

  let drafts = $state<Record<string, string>>({});
  let errors = $state<Record<string, string>>({});
  let busyKeys = $state<string[]>([]);
  let refreshError = $state('');
  let refreshTimer: ReturnType<typeof setInterval> | null = null;
  let mutationVersion = 0;
  let refreshRequestId = 0;
  let groups = $derived(groupSystemSettings(settings));
  let hasDirtyDrafts = $derived(Object.entries(drafts).some(([key, draft]) => {
    const setting = settings
      .flatMap((category: SettingsCategory) => category.items)
      .find((item: Setting) => item.key === key);
    return setting ? draft !== serializeSettingValue(setting) : false;
  }));

  $effect(() => {
    ondirtychange?.(hasDirtyDrafts);
  });

  function draftFor(setting: Setting): string {
    return drafts[setting.key] ?? serializeSettingValue(setting);
  }

  function updateDraft(setting: Setting, draft: string): void {
    if (draft === serializeSettingValue(setting)) {
      clearDraft(setting.key);
      return;
    }
    drafts = { ...drafts, [setting.key]: draft };
    const parsed = parseSettingDraft(setting, draft);
    const nextErrors = { ...errors };
    if (parsed.error) nextErrors[setting.key] = parsed.error;
    else delete nextErrors[setting.key];
    errors = nextErrors;
  }

  function clearDraft(key: string): void {
    const nextDrafts = { ...drafts };
    const nextErrors = { ...errors };
    delete nextDrafts[key];
    delete nextErrors[key];
    drafts = nextDrafts;
    errors = nextErrors;
  }

  function setBusy(key: string, busy: boolean): void {
    busyKeys = busy
      ? [...new Set([...busyKeys, key])]
      : busyKeys.filter((candidate) => candidate !== key);
  }

  function removeDraftsMatching(refreshed: SettingsCategory[]): void {
    const refreshedByKey = new Map(
      refreshed.flatMap((category) => category.items).map((setting) => [setting.key, setting])
    );
    const nextDrafts = { ...drafts };
    const nextErrors = { ...errors };
    let changed = false;
    for (const [key, draft] of Object.entries(drafts)) {
      const setting = refreshedByKey.get(key);
      if (setting && draft === serializeSettingValue(setting)) {
        delete nextDrafts[key];
        delete nextErrors[key];
        changed = true;
      }
    }
    if (changed) {
      drafts = nextDrafts;
      errors = nextErrors;
    }
  }

  async function applySetting(setting: Setting): Promise<void> {
    const parsed = parseSettingDraft(setting, draftFor(setting));
    if (parsed.error) {
      errors = { ...errors, [setting.key]: parsed.error };
      return;
    }

    mutationVersion += 1;
    setBusy(setting.key, true);
    try {
      const updated = await api.settings.update(setting.key, parsed.value);
      clearDraft(setting.key);
      onsettingschange(replaceSetting(settings, updated));
      addToast(`${updated.label || updated.key} updated.`, 'success');
    } catch (caughtError) {
      const message = asApiError(caughtError).message;
      errors = { ...errors, [setting.key]: message };
      addToast(message, 'error', 4_000, 'Unable to update setting');
    } finally {
      setBusy(setting.key, false);
    }
  }

  async function resetSetting(setting: Setting): Promise<void> {
    const confirmed = await confirmAction({
      title: 'Reset setting to default?',
      message: `${setting.label || setting.key} will use its default value (${JSON.stringify(setting.default_value)}).`,
      confirmLabel: 'Reset to default'
    });
    if (!confirmed) return;

    mutationVersion += 1;
    setBusy(setting.key, true);
    try {
      const updated = await api.settings.reset(setting.key);
      clearDraft(setting.key);
      onsettingschange(replaceSetting(settings, updated));
      addToast(`${updated.label || updated.key} reset to default.`, 'success');
    } catch (caughtError) {
      const message = asApiError(caughtError).message;
      errors = { ...errors, [setting.key]: message };
      addToast(message, 'error', 4_000, 'Unable to reset setting');
    } finally {
      setBusy(setting.key, false);
    }
  }

  async function refreshSettings(): Promise<void> {
    if (document.visibilityState !== 'visible' || busyKeys.length > 0) return;
    const requestId = ++refreshRequestId;
    const versionAtStart = mutationVersion;
    try {
      const refreshed = await api.settings.list();
      if (requestId !== refreshRequestId || versionAtStart !== mutationVersion) return;
      removeDraftsMatching(refreshed);
      onsettingschange(refreshed);
      refreshError = '';
    } catch (caughtError) {
      if (requestId !== refreshRequestId || versionAtStart !== mutationVersion) return;
      refreshError = asApiError(caughtError).message;
    }
  }

  function onVisibilityChange(): void {
    if (document.visibilityState === 'visible') void refreshSettings();
  }

  function sectionId(category: string, section: string): string {
    return `settings-section-${category}-${section}`.toLowerCase().replace(/[^a-z0-9_-]+/g, '-');
  }

  onMount(() => {
    document.addEventListener('visibilitychange', onVisibilityChange);
    window.addEventListener('focus', refreshSettings);
    refreshTimer = setInterval(() => void refreshSettings(), REFRESH_INTERVAL_MS);
  });

  onDestroy(() => {
    document.removeEventListener('visibilitychange', onVisibilityChange);
    window.removeEventListener('focus', refreshSettings);
    if (refreshTimer) clearInterval(refreshTimer);
    ondirtychange?.(false);
  });
</script>

<div class="space-y-5" data-testid="system-settings-editor">
  <div class="flex flex-wrap items-end justify-between gap-3">
    <div>
      <p class="text-xs uppercase tracking-[0.25em] text-slate-400">Runtime configuration</p>
      <h2 class="mt-1 text-xl font-semibold text-white">System settings</h2>
      <p class="mt-2 max-w-3xl text-sm text-slate-400">
        Changes are applied per setting. Unsaved rows remain intact while values refresh in the background.
      </p>
    </div>
    {#if refreshError}
      <p class="text-sm text-amber-300" role="status">Automatic refresh failed: {refreshError}</p>
    {/if}
  </div>

  {#each groups as category (category.name)}
    <Card class="p-4 sm:p-5">
      <h3 class="text-lg font-semibold capitalize text-white">{category.name}</h3>
      <div class="mt-4 space-y-5">
        {#each category.sections as section (`${category.name}:${section.name}`)}
          <section aria-labelledby={sectionId(category.name, section.name)} class="space-y-3">
            <div class="border-b border-slate-800 pb-2">
              <h4 id={sectionId(category.name, section.name)} class="text-sm font-semibold text-slate-200">{section.name}</h4>
            </div>
            <div class="grid gap-3 xl:grid-cols-2">
              {#each section.items as setting (setting.key)}
                <SystemSettingRow
                  {setting}
                  draft={draftFor(setting)}
                  error={errors[setting.key] ?? ''}
                  busy={busyKeys.includes(setting.key)}
                  {disabled}
                  onchange={(draft) => updateDraft(setting, draft)}
                  onapply={() => void applySetting(setting)}
                  onreset={() => void resetSetting(setting)}
                />
              {/each}
            </div>
          </section>
        {/each}
      </div>
    </Card>
  {/each}

  {#if groups.length === 0}
    <Card class="p-6 text-center text-sm text-slate-400">No system settings are available.</Card>
  {/if}
</div>
