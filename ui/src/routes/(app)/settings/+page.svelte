<script lang="ts">
  import { goto } from '$app/navigation';
  import { onMount } from 'svelte';

  import { api, asApiError } from '$lib/api/client';
  import LoadingState from '$lib/components/LoadingState.svelte';
  import ProviderStatusBadge from '$lib/components/ProviderStatusBadge.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import Card from '$lib/components/ui/Card.svelte';
  import Input from '$lib/components/ui/Input.svelte';
  import { getIntarisUiUrl, getMnemoryUiUrl } from '$lib/config';
  import { auth } from '$lib/stores/auth';
  import type { HealthResponse, LLMProvider, ModelRouting, SecretMetadata, Setting, SettingsCategory } from '$lib/types/api';

  type SettingsTab = 'providers' | 'routing' | 'secrets' | 'system' | 'account';

  const tabs: SettingsTab[] = ['providers', 'routing', 'secrets', 'system', 'account'];

  let activeTab: SettingsTab = 'providers';
  let loading = true;
  let error = '';
  let settings: SettingsCategory[] = [];
  let providers: LLMProvider[] = [];
  let modelRouting: ModelRouting = { default: null, classifier: null, compaction: null, simple_inline: null, items: {} };
  let secrets: SecretMetadata[] = [];
  let health: HealthResponse | null = null;
  let isAdmin = false;
  let selectedProviderId = '';
  let selectedSettingKey = '';
  let settingValueText = '';

  let providerForm = {
    provider_id: '',
    display_name: '',
    location: 'controller',
    backend: 'litellm',
    configJson: '{}',
    status: 'active'
  };

  let routingForm = {
    default: '',
    classifier: '',
    compaction: '',
    simple_inline: '',
    extraJson: '{}'
  };

  let secretForm = {
    name: '',
    value: '',
    scope: 'user',
    agent_id: '',
    description: ''
  };

  function groupedSettings(): Setting[] {
    return settings.flatMap((group) => group.items);
  }

  function selectProvider(provider: LLMProvider): void {
    selectedProviderId = provider.provider_id;
    providerForm = {
      provider_id: provider.provider_id,
      display_name: provider.display_name,
      location: provider.location,
      backend: provider.backend,
      configJson: JSON.stringify(provider.config, null, 2),
      status: provider.status
    };
  }

  function resetProviderForm(): void {
    selectedProviderId = '';
    providerForm = {
      provider_id: '',
      display_name: '',
      location: 'controller',
      backend: 'litellm',
      configJson: '{}',
      status: 'active'
    };
  }

  async function loadSettings(): Promise<void> {
    loading = true;
    error = '';
    isAdmin = auth.getSnapshot().user?.role === 'admin';

    try {
      [settings, modelRouting, secrets, health] = await Promise.all([
        api.settings.list(),
        api.modelRouting.get(),
        api.secrets.list(),
        api.system.health()
      ]);

      routingForm = {
        default: modelRouting.default ?? '',
        classifier: modelRouting.classifier ?? '',
        compaction: modelRouting.compaction ?? '',
        simple_inline: modelRouting.simple_inline ?? '',
        extraJson: JSON.stringify(modelRouting.items, null, 2)
      };

      if (isAdmin) {
        providers = (await api.llmProviders.list()).items;
      }
    } catch (caughtError) {
      error = asApiError(caughtError).message;
    } finally {
      loading = false;
    }
  }

  async function saveProvider(): Promise<void> {
    try {
      const payload = {
        provider_id: providerForm.provider_id,
        display_name: providerForm.display_name,
        location: providerForm.location,
        backend: providerForm.backend,
        config: JSON.parse(providerForm.configJson || '{}'),
        status: providerForm.status
      };

      if (selectedProviderId) {
        await api.llmProviders.update(selectedProviderId, payload);
      } else {
        await api.llmProviders.create(payload);
      }
      resetProviderForm();
      providers = (await api.llmProviders.list()).items;
    } catch (caughtError) {
      error = asApiError(caughtError).message;
    }
  }

  async function deleteProvider(providerId: string): Promise<void> {
    try {
      await api.llmProviders.remove(providerId);
      resetProviderForm();
      providers = (await api.llmProviders.list()).items;
    } catch (caughtError) {
      error = asApiError(caughtError).message;
    }
  }

  async function testProvider(providerId: string): Promise<void> {
    try {
      await api.llmProviders.test(providerId);
    } catch (caughtError) {
      error = asApiError(caughtError).message;
    }
  }

  async function saveRouting(): Promise<void> {
    try {
      modelRouting = await api.modelRouting.update({
        default: routingForm.default || null,
        classifier: routingForm.classifier || null,
        compaction: routingForm.compaction || null,
        simple_inline: routingForm.simple_inline || null,
        items: JSON.parse(routingForm.extraJson || '{}')
      });
    } catch (caughtError) {
      error = asApiError(caughtError).message;
    }
  }

  async function saveSecret(): Promise<void> {
    try {
      await api.secrets.upsert({
        name: secretForm.name,
        value: secretForm.value,
        scope: secretForm.scope,
        agent_id: secretForm.agent_id || null,
        description: secretForm.description || null
      });
      secretForm = { ...secretForm, name: '', value: '', agent_id: '', description: '' };
      secrets = await api.secrets.list();
    } catch (caughtError) {
      error = asApiError(caughtError).message;
    }
  }

  async function deleteSecret(secret: SecretMetadata): Promise<void> {
    try {
      await api.secrets.remove(secret.name, secret.scope, secret.agent_id);
      secrets = await api.secrets.list();
    } catch (caughtError) {
      error = asApiError(caughtError).message;
    }
  }

  function selectSetting(setting: Setting): void {
    selectedSettingKey = setting.key;
    settingValueText = JSON.stringify(setting.value, null, 2);
  }

  async function saveSetting(): Promise<void> {
    try {
      await api.settings.update(selectedSettingKey, JSON.parse(settingValueText));
      settings = await api.settings.list();
    } catch (caughtError) {
      error = asApiError(caughtError).message;
    }
  }

  async function openTargetUi(target: 'intaris' | 'mnemory'): Promise<void> {
    try {
      const exchange = await api.auth.exchangeToken(target);
      const baseUrl = target === 'intaris' ? getIntarisUiUrl() : getMnemoryUiUrl();
      const url = new URL(baseUrl, window.location.origin);
      url.searchParams.set('token', exchange.token);
      window.open(url.toString(), '_blank', 'noopener,noreferrer');
    } catch (caughtError) {
      error = asApiError(caughtError).message;
    }
  }

  onMount(() => {
    void loadSettings();
  });
</script>

<svelte:head>
  <title>Settings · Cognis</title>
</svelte:head>

{#if loading}
  <LoadingState label="Loading settings" description="Fetching providers, routing, secrets, health, and your account profile." />
{:else}
  <section class="space-y-5">
    <div class="flex flex-wrap gap-2">
      {#each tabs as tab}
        <Button variant={activeTab === tab ? 'primary' : 'secondary'} onclick={() => (activeTab = tab)}>{tab}</Button>
      {/each}
    </div>

    {#if error}
      <p class="rounded-2xl border border-rose-500/30 bg-rose-500/10 px-4 py-3 text-sm text-rose-100">{error}</p>
    {/if}

    {#if activeTab === 'providers'}
      <div class="grid gap-5 xl:grid-cols-[360px_minmax(0,1fr)]">
        <Card class="p-5">
          <div class="space-y-4">
            <div>
              <p class="text-xs uppercase tracking-[0.25em] text-slate-400">Providers</p>
              <h2 class="mt-1 text-lg font-semibold text-white">LLM providers</h2>
            </div>
            {#if !isAdmin}
              <p class="text-sm text-slate-400">Provider management is available to admin users only.</p>
            {:else}
              {#each providers as provider}
                <button class="flex w-full items-center justify-between rounded-2xl border border-slate-800 bg-slate-950/70 px-4 py-3 text-left text-slate-200" onclick={() => selectProvider(provider)}>
                  <span>{provider.display_name}</span>
                  <ProviderStatusBadge status={provider.status} />
                </button>
              {/each}
            {/if}
          </div>
        </Card>

        <Card class="p-5">
          <div class="grid gap-4 md:grid-cols-2">
            <label class="space-y-2 text-sm font-medium text-slate-200">
              <span>Provider ID</span>
              <Input bind:value={providerForm.provider_id} disabled={!!selectedProviderId} />
            </label>
            <label class="space-y-2 text-sm font-medium text-slate-200">
              <span>Display name</span>
              <Input bind:value={providerForm.display_name} />
            </label>
            <label class="space-y-2 text-sm font-medium text-slate-200">
              <span>Location</span>
              <Input bind:value={providerForm.location} />
            </label>
            <label class="space-y-2 text-sm font-medium text-slate-200">
              <span>Backend</span>
              <Input bind:value={providerForm.backend} />
            </label>
          </div>
          <label class="mt-4 block space-y-2 text-sm font-medium text-slate-200">
            <span>Config (JSON)</span>
            <textarea bind:value={providerForm.configJson} class="min-h-[220px] w-full rounded-2xl border border-slate-700 bg-slate-950/80 px-4 py-3 font-mono text-sm text-slate-100"></textarea>
          </label>
          <div class="mt-5 flex flex-wrap gap-2">
            <Button onclick={saveProvider} disabled={!isAdmin}>{selectedProviderId ? 'Save provider' : 'Create provider'}</Button>
            <Button variant="secondary" onclick={resetProviderForm} disabled={!isAdmin}>Reset</Button>
            {#if selectedProviderId}
              <Button variant="secondary" onclick={() => testProvider(selectedProviderId)} disabled={!isAdmin}>Test</Button>
              <Button variant="danger" onclick={() => deleteProvider(selectedProviderId)} disabled={!isAdmin}>Delete</Button>
            {/if}
          </div>
        </Card>
      </div>
    {:else if activeTab === 'routing'}
      <Card class="p-5">
        <div class="grid gap-4 md:grid-cols-2">
          <label class="space-y-2 text-sm font-medium text-slate-200"><span>Default</span><Input bind:value={routingForm.default} /></label>
          <label class="space-y-2 text-sm font-medium text-slate-200"><span>Classifier</span><Input bind:value={routingForm.classifier} /></label>
          <label class="space-y-2 text-sm font-medium text-slate-200"><span>Compaction</span><Input bind:value={routingForm.compaction} /></label>
          <label class="space-y-2 text-sm font-medium text-slate-200"><span>Simple inline</span><Input bind:value={routingForm.simple_inline} /></label>
        </div>
        <label class="mt-4 block space-y-2 text-sm font-medium text-slate-200">
          <span>Additional task routes (JSON)</span>
          <textarea bind:value={routingForm.extraJson} class="min-h-[220px] w-full rounded-2xl border border-slate-700 bg-slate-950/80 px-4 py-3 font-mono text-sm text-slate-100"></textarea>
        </label>
        <div class="mt-5 flex justify-end">
          <Button onclick={saveRouting} disabled={!isAdmin}>Save routing</Button>
        </div>
      </Card>
    {:else if activeTab === 'secrets'}
      <div class="grid gap-5 xl:grid-cols-[360px_minmax(0,1fr)]">
        <Card class="p-5">
          <div class="space-y-4">
            <label class="space-y-2 text-sm font-medium text-slate-200"><span>Name</span><Input bind:value={secretForm.name} /></label>
            <label class="space-y-2 text-sm font-medium text-slate-200"><span>Value</span><Input bind:value={secretForm.value} type="password" placeholder="write-only secret" /></label>
            <label class="space-y-2 text-sm font-medium text-slate-200"><span>Scope</span><Input bind:value={secretForm.scope} /></label>
            <label class="space-y-2 text-sm font-medium text-slate-200"><span>Agent ID</span><Input bind:value={secretForm.agent_id} /></label>
            <label class="space-y-2 text-sm font-medium text-slate-200"><span>Description</span><Input bind:value={secretForm.description} /></label>
            <Button class="w-full justify-center" onclick={saveSecret}>Save secret</Button>
          </div>
        </Card>
        <Card class="p-5">
          <div class="space-y-3">
            {#each secrets as secret}
              <div class="flex items-center justify-between gap-3 rounded-2xl border border-slate-800 bg-slate-950/70 px-4 py-3">
                <div>
                  <p class="font-medium text-white">{secret.name}</p>
                  <p class="text-xs text-slate-400">{secret.scope}{secret.agent_id ? ` · ${secret.agent_id}` : ''}</p>
                </div>
                <Button size="sm" variant="danger" onclick={() => deleteSecret(secret)}>Delete</Button>
              </div>
            {/each}
          </div>
        </Card>
      </div>
    {:else if activeTab === 'system'}
      <div class="grid gap-5 xl:grid-cols-[360px_minmax(0,1fr)]">
        <Card class="p-5">
          <div class="space-y-3">
            <p class="text-xs uppercase tracking-[0.25em] text-slate-400">Health</p>
            {#if health}
              {#each Object.entries(health.providers) as [name, provider]}
                <div class="flex items-center justify-between gap-3 rounded-2xl border border-slate-800 bg-slate-950/70 px-4 py-3">
                  <span class="font-medium text-white">{name}</span>
                  <ProviderStatusBadge status={provider.status} />
                </div>
              {/each}
            {/if}
          </div>
        </Card>
        <Card class="p-5">
          <div class="space-y-3">
            <p class="text-xs uppercase tracking-[0.25em] text-slate-400">Settings</p>
            <div class="grid gap-3 md:grid-cols-2">
              {#each groupedSettings() as setting}
                <button class="rounded-2xl border border-slate-800 bg-slate-950/70 px-4 py-3 text-left text-slate-200" onclick={() => selectSetting(setting)}>
                  <p class="font-medium">{setting.key}</p>
                  <p class="mt-1 text-xs text-slate-400">{setting.category}</p>
                </button>
              {/each}
            </div>
            {#if selectedSettingKey}
              <div class="space-y-3 border-t border-slate-800 pt-4">
                <p class="text-sm font-medium text-white">Edit {selectedSettingKey}</p>
                <textarea bind:value={settingValueText} class="min-h-[220px] w-full rounded-2xl border border-slate-700 bg-slate-950/80 px-4 py-3 font-mono text-sm text-slate-100"></textarea>
                <Button onclick={saveSetting} disabled={!isAdmin}>Save setting</Button>
              </div>
            {/if}
          </div>
        </Card>
      </div>
    {:else}
      <Card class="p-5">
        <div class="space-y-4">
          <div>
            <p class="text-xs uppercase tracking-[0.25em] text-slate-400">Account</p>
            <h2 class="mt-1 text-lg font-semibold text-white">{auth.getSnapshot().user?.name ?? auth.getSnapshot().user?.email}</h2>
            <p class="text-sm text-slate-400">{auth.getSnapshot().user?.email}</p>
          </div>
          <div class="flex flex-wrap gap-2">
            <Button variant="secondary" onclick={() => openTargetUi('intaris')}>Open Intaris</Button>
            <Button variant="secondary" onclick={() => openTargetUi('mnemory')}>Open Mnemory</Button>
            <Button variant="danger" onclick={async () => { await auth.logout(); await goto('/login'); }}>Sign out</Button>
          </div>
          <p class="text-sm leading-6 text-slate-400">
            API key management and password change endpoints are not yet exposed by the current backend.
          </p>
        </div>
      </Card>
    {/if}
  </section>
{/if}
