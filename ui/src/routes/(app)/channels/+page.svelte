<script lang="ts">
  import { onMount } from 'svelte';

  import { api, asApiError } from '$lib/api/client';
  import {
    buildSummaryCards,
    createChannelDraft,
    getAgentName,
    getChannelGuide,
    getChannelMetaByType,
    getPendingPairingCount,
    normalizeSettingValue,
    type ChannelEditorMode,
    type ChannelEditorDraft,
    type ChannelsTab,
  } from '$lib/channels';
  import LoadingState from '$lib/components/LoadingState.svelte';
  import Card from '$lib/components/ui/Card.svelte';
  import { confirmAction } from '$lib/stores/confirm';
  import { registerOverlay } from '$lib/stores/overlays';
  import { addToast } from '$lib/stores/toasts';
  import type { Agent, ChannelAccount, ChannelContact, ChannelMeta, ExecutorConfig, PairingRequest } from '$lib/types/api';

  import ChannelAccountEditor from './ChannelAccountEditor.svelte';
  import ChannelAccountsView from './ChannelAccountsView.svelte';
  import ChannelsPageHeader from './ChannelsPageHeader.svelte';
  import ChannelsSummaryCards from './ChannelsSummaryCards.svelte';
  import ContactsView from './ContactsView.svelte';
  import PairingInboxView from './PairingInboxView.svelte';

  let loading = $state(true);
  let busy = $state(false);
  let error = $state('');
  let activeTab = $state<ChannelsTab>('accounts');

  let channelTypes = $state<ChannelMeta[]>([]);
  let accounts = $state<ChannelAccount[]>([]);
  let contacts = $state<ChannelContact[]>([]);
  let pairingRequests = $state<PairingRequest[]>([]);
  let agents = $state<Agent[]>([]);
  let executors = $state<ExecutorConfig[]>([]);

  let selectedAccountId = $state<string | null>(null);
  let editorMode = $state<ChannelEditorMode>('closed');
  let selectedType = $state<ChannelMeta | null>(null);
  let draft = $state<ChannelEditorDraft>({
    display_name: '',
    agent_id: '',
    adapter_location: 'controller',
    executor_id: '',
    dm_policy: 'pairing',
    group_policy: 'pairing',
    allow_new_conversations: true,
    credentialValues: {},
    settingValues: {},
  });
  let credentialOverrides = $state<Record<string, string>>({});
  let initialDraftSnapshot = $state('');
  let mobileEditorOpen = $state(false);
  let editorPreviouslyFocused = $state<HTMLElement | null>(null);
  let mobileEditorOverlayCleanup: (() => void) | null = null;

  let webhookInfo = $state<{ url: string; secret: string | null; channelType: string } | null>(null);
  let webhookInfoDismissed = $state(false);

  let contactForm = $state({
    channel_type: 'signal',
    sender_id: '',
    display_name: '',
  });

  let redeemCode = $state('');

  const emptyDraft = (): ChannelEditorDraft => ({
    display_name: '',
    agent_id: '',
    adapter_location: 'controller',
    executor_id: '',
    dm_policy: 'pairing',
    group_policy: 'pairing',
    allow_new_conversations: true,
    credentialValues: {},
    settingValues: {},
  });

  const summaryCards = $derived(buildSummaryCards(accounts, contacts, pairingRequests, !loading));
  const isDirty = $derived(
    editorMode !== 'closed' && initialDraftSnapshot !== '' && JSON.stringify(draft) !== initialDraftSnapshot,
  );
  const metaMap = $derived(
    Object.fromEntries(channelTypes.map((meta) => [meta.channel_type, meta])) as Record<string, ChannelMeta>,
  );

  function currentOrigin(): string {
    return typeof window === 'undefined' ? '' : window.location.origin;
  }

  function selectedGuide() {
    return getChannelGuide(selectedType?.channel_type);
  }

  function accountPendingCount(accountId: string): number {
    return getPendingPairingCount(accountId, pairingRequests);
  }

  function rememberDraftSnapshot(): void {
    initialDraftSnapshot = JSON.stringify(draft);
  }

  function usesMobileEditorOverlay(): boolean {
    // Aligns with Tailwind's `lg` breakpoint (1024px) and the new app-wide
    // mobile/desktop pivot.
    return typeof window !== 'undefined' && window.innerWidth < 1024;
  }

  function openEditor(): void {
    editorPreviouslyFocused = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    mobileEditorOpen = usesMobileEditorOverlay();
  }

  function closeEditor(): void {
    editorMode = 'closed';
    selectedAccountId = null;
    selectedType = null;
    draft = emptyDraft();
    credentialOverrides = {};
    mobileEditorOpen = false;
    initialDraftSnapshot = '';
    editorPreviouslyFocused?.focus();
    editorPreviouslyFocused = null;
  }

  $effect(() => {
    if (mobileEditorOpen) {
      const handle = registerOverlay({ kind: 'sheet', blocksChrome: false });
      mobileEditorOverlayCleanup = handle.unregister;
      return () => {
        handle.unregister();
        mobileEditorOverlayCleanup = null;
      };
    }

    mobileEditorOverlayCleanup?.();
    mobileEditorOverlayCleanup = null;
  });

  function handleTabChange(tab: ChannelsTab): void {
    activeTab = tab;
    if (tab !== 'accounts' && mobileEditorOpen) {
      closeEditor();
    }
    if (tab === 'pairing') {
      void refreshPairingRequests();
    }
  }

  function beginCreate(meta: ChannelMeta): void {
    selectedAccountId = null;
    selectedType = meta;
    editorMode = 'create';
    draft = createChannelDraft(meta, agents);
    credentialOverrides = {};
    rememberDraftSnapshot();
    webhookInfo = null;
    webhookInfoDismissed = false;
    activeTab = 'accounts';
    openEditor();
  }

  function beginCreateDefault(): void {
    const fallback = selectedType ?? channelTypes[0] ?? null;
    if (fallback) {
      beginCreate(fallback);
    }
  }

  function beginEdit(account: ChannelAccount): void {
    const meta = getChannelMetaByType(channelTypes, account.channel_type);
    if (!meta) {
      return;
    }
    selectedAccountId = account.account_id;
    selectedType = meta;
    editorMode = 'edit';
    draft = createChannelDraft(meta, agents, account);
    credentialOverrides = {};
    rememberDraftSnapshot();
    webhookInfo = null;
    webhookInfoDismissed = true;
    activeTab = 'accounts';
    openEditor();
  }

  async function loadData(): Promise<void> {
    loading = true;
    error = '';
    try {
      const [types, accountsResult, contactsResult, pairingResult, agentsResult, executorsResult] = await Promise.all([
        api.channels.listTypes(),
        api.channels.listAccounts(),
        api.channels.listContacts(),
        api.channels.listPairingRequests(),
        api.agents.listAll({ include_hidden: false, include_system: true }),
        api.executor.list().catch(() => []),
      ]);
      channelTypes = types;
      accounts = accountsResult;
      contacts = contactsResult;
      pairingRequests = pairingResult;
      agents = agentsResult.filter((agent) => agent.status !== 'archived');
      executors = (executorsResult as ExecutorConfig[]) ?? [];
      if (!selectedType && types.length > 0) {
        selectedType = types[0];
      }
      if (contactForm.channel_type === 'signal' && types.length > 0) {
        contactForm.channel_type = types[0].channel_type;
      }
    } catch (caughtError) {
      error = asApiError(caughtError).message;
    } finally {
      loading = false;
    }
  }

  async function refreshPairingRequests(): Promise<void> {
    try {
      pairingRequests = await api.channels.listPairingRequests();
    } catch {
      // Keep the last good snapshot.
    }
  }

  async function saveAccount(): Promise<void> {
    if (!selectedType) {
      return;
    }
    if (!draft.agent_id) {
      addToast('Select an agent for this channel account.', 'error');
      return;
    }

    busy = true;
    error = '';
    try {
      const credential_refs: Record<string, string> = {};
      const settings: Record<string, unknown> = {};

      for (const field of selectedType.credential_fields) {
        const rawValue = (draft.credentialValues[field.name] ?? '').trim();
        if (field.required && !rawValue) {
          throw new Error(`${field.label} is required.`);
        }
        if (!rawValue) {
          continue;
        }
        if (field.secret) {
          const secretName = `channel.${selectedType.channel_type}.${Date.now()}.${field.name}`;
          await api.secrets.upsert({
            name: secretName,
            value: rawValue,
            scope: 'user',
            agent_id: null,
            description: `${selectedType.label} credential: ${field.label}`,
          });
          credential_refs[field.name] = secretName;
        } else {
          settings[field.name] = rawValue;
        }
      }

      for (const field of selectedType.setting_fields) {
        const rawValue = draft.settingValues[field.name] ?? '';
        if (rawValue === '') {
          continue;
        }
        settings[field.name] = normalizeSettingValue(selectedType, field.name, rawValue);
      }

      const created = await api.channels.createAccount({
        channel_type: selectedType.channel_type,
        display_name: draft.display_name,
        agent_id: draft.agent_id,
        settings,
        credential_refs,
        adapter_location: draft.adapter_location,
        executor_id: draft.executor_id || null,
        dm_policy: draft.dm_policy,
        group_policy: draft.group_policy,
        allow_new_conversations: draft.allow_new_conversations,
      });

      webhookInfo = selectedGuide()?.publicUrlNeeded
        ? {
            channelType: created.channel_type,
            url: `${currentOrigin()}/api/v1/channels/webhook/${created.channel_type}/${created.account_id}`,
            secret: created.webhook_secret,
          }
        : null;
      webhookInfoDismissed = false;

      addToast('Channel account created.', 'success');
      try {
        await loadData();
      } catch (caughtError) {
        error = asApiError(caughtError).message;
      }
      closeEditor();
    } catch (caughtError) {
      const message = asApiError(caughtError).message;
      error = message;
      addToast(message, 'error', 4000, 'Unable to create channel account');
    } finally {
      busy = false;
    }
  }

  async function saveAccountChanges(): Promise<void> {
    if (!selectedType || !selectedAccountId) {
      return;
    }

    busy = true;
    error = '';
    try {
      const current = accounts.find((account) => account.account_id === selectedAccountId);
      if (!current) {
        throw new Error('Channel account no longer exists.');
      }

      const updates: Record<string, unknown> = {
        display_name: draft.display_name,
        agent_id: draft.agent_id,
        adapter_location: draft.adapter_location,
        executor_id: draft.executor_id || null,
        dm_policy: draft.dm_policy,
        group_policy: draft.group_policy,
        allow_new_conversations: draft.allow_new_conversations,
      };

      const config: Record<string, unknown> = {};
      for (const field of selectedType.setting_fields) {
        const rawValue = draft.settingValues[field.name] ?? '';
        if (rawValue === '') {
          continue;
        }
        config[field.name] = normalizeSettingValue(selectedType, field.name, rawValue);
      }
      updates.config = config;

      const credential_refs: Record<string, string> = { ...(current.credential_refs ?? {}) };
      const replacedSecretNames: string[] = [];
      for (const field of selectedType.credential_fields) {
        if (field.secret) {
          const replacement = (credentialOverrides[field.name] ?? '').trim();
          if (!replacement) {
            continue;
          }
          const secretName = `channel.${selectedType.channel_type}.${Date.now()}.${field.name}`;
          await api.secrets.upsert({
            name: secretName,
            value: replacement,
            scope: 'user',
            agent_id: null,
            description: `${selectedType.label} credential: ${field.label}`,
          });
          const previousSecret = credential_refs[field.name];
          if (previousSecret && previousSecret !== secretName) {
            replacedSecretNames.push(previousSecret);
          }
          credential_refs[field.name] = secretName;
        } else {
          const value = (draft.credentialValues[field.name] ?? '').trim();
          if (value) {
            (updates.config as Record<string, unknown>)[field.name] = value;
          }
        }
      }
      updates.credential_refs = credential_refs;

      await api.channels.updateAccount(selectedAccountId, updates);
      for (const secretName of replacedSecretNames) {
        try {
          await api.secrets.remove(secretName, 'user', null);
        } catch {
          // Best effort cleanup.
        }
      }
      addToast('Channel account updated.', 'success');
      await loadData();
      closeEditor();
    } catch (caughtError) {
      addToast(asApiError(caughtError).message, 'error', 4000, 'Unable to update channel account');
    } finally {
      busy = false;
    }
  }

  async function toggleAccount(account: ChannelAccount): Promise<void> {
    busy = true;
    try {
      if ((account.status && 'status' in account.status ? account.status.status : 'stopped') === 'connected') {
        await api.channels.stopAccount(account.account_id);
        addToast('Channel stopped.', 'success');
      } else {
        await api.channels.startAccount(account.account_id);
        addToast('Channel start requested.', 'success');
      }
      accounts = await api.channels.listAccounts();
    } catch (caughtError) {
      addToast(asApiError(caughtError).message, 'error', 4000, 'Unable to change channel status');
    } finally {
      busy = false;
    }
  }

  async function removeAccount(account: ChannelAccount): Promise<void> {
    const confirmed = await confirmAction({
      title: 'Delete channel account?',
      message: `This removes ${account.display_name} and stops its adapter.`,
      confirmLabel: 'Delete account',
    });
    if (!confirmed) {
      return;
    }
    busy = true;
    try {
      await api.channels.deleteAccount(account.account_id);
      addToast('Channel account deleted.', 'success');
      await loadData();
      if (selectedAccountId === account.account_id) {
        closeEditor();
      }
    } catch (caughtError) {
      addToast(asApiError(caughtError).message, 'error', 4000, 'Unable to delete channel account');
    } finally {
      busy = false;
    }
  }

  async function saveContact(): Promise<void> {
    if (!contactForm.sender_id.trim()) {
      addToast('Sender ID is required.', 'error');
      return;
    }
    busy = true;
    try {
      await api.channels.createContact({
        channel_type: contactForm.channel_type,
        sender_id: contactForm.sender_id.trim(),
        display_name: contactForm.display_name.trim() || null,
        verified: true,
      });
      contactForm.sender_id = '';
      contactForm.display_name = '';
      contacts = await api.channels.listContacts();
      addToast('Verified sender saved.', 'success');
    } catch (caughtError) {
      addToast(asApiError(caughtError).message, 'error', 4000, 'Unable to save sender');
    } finally {
      busy = false;
    }
  }

  async function redeemPairingCode(): Promise<void> {
    if (!redeemCode.trim()) {
      addToast('Enter the pairing code shown in the chat.', 'error');
      return;
    }
    busy = true;
    try {
      await api.channels.redeemPairingCode(redeemCode.trim());
      redeemCode = '';
      await loadData();
      addToast('Sender paired successfully.', 'success');
    } catch (caughtError) {
      addToast(asApiError(caughtError).message, 'error', 4000, 'Unable to redeem pairing code');
    } finally {
      busy = false;
    }
  }

  async function rejectPairing(requestId: string): Promise<void> {
    const confirmed = await confirmAction({
      title: 'Reject pairing request?',
      message: 'The remote sender will stay blocked until they request a new code.',
      confirmLabel: 'Reject request',
    });
    if (!confirmed) {
      return;
    }
    busy = true;
    try {
      await api.channels.rejectPairingRequest(requestId);
      await refreshPairingRequests();
      addToast('Pairing request rejected.', 'success');
    } catch (caughtError) {
      addToast(asApiError(caughtError).message, 'error', 4000, 'Unable to reject pairing request');
    } finally {
      busy = false;
    }
  }

  async function copyWebhookInfo(value: string): Promise<void> {
    try {
      await navigator.clipboard.writeText(value);
      addToast('Copied to clipboard.', 'success');
    } catch {
      addToast('Unable to copy to clipboard.', 'error');
    }
  }

  onMount(() => {
    void loadData();
    const interval = window.setInterval(() => {
      if (activeTab === 'pairing') {
        void refreshPairingRequests();
      }
    }, 10_000);
    return () => {
      mobileEditorOverlayCleanup?.();
      mobileEditorOverlayCleanup = null;
      window.clearInterval(interval);
    };
  });
</script>

{#if loading}
  <LoadingState label="Loading channels" description="Fetching channel accounts, pairing requests, and trusted senders." />
{:else}
  <div class="space-y-6">
    <ChannelsPageHeader busy={busy} onRefresh={() => void loadData()} onAdd={beginCreateDefault} />

    {#if error}
      <Card class="border border-rose-500/20 bg-rose-500/10 p-4 text-sm text-rose-200">{error}</Card>
    {/if}

    <ChannelsSummaryCards items={summaryCards} />

    {#if webhookInfo && !webhookInfoDismissed}
      <Card class="border border-emerald-500/20 bg-emerald-500/10 p-5 text-sm text-emerald-100">
        <div class="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
          <div>
            <p class="font-medium">Webhook setup for {webhookInfo.channelType}</p>
            <p class="mt-2 text-emerald-100/80">Use this callback URL in the external platform configuration:</p>
            <code class="mt-3 block rounded-xl bg-slate-950/80 px-3 py-2 text-xs text-slate-100">{webhookInfo.url}</code>
            {#if webhookInfo.secret}
              <p class="mt-3 text-emerald-100/80">Verification secret / token:</p>
              <code class="mt-2 block rounded-xl bg-slate-950/80 px-3 py-2 text-xs text-slate-100">{webhookInfo.secret}</code>
            {/if}
          </div>
          <div class="flex flex-col gap-2">
            <button class="rounded-xl border border-slate-700 bg-slate-950/60 px-3 py-2 text-sm text-slate-100" onclick={() => webhookInfo && void copyWebhookInfo(webhookInfo.url)} type="button">Copy URL</button>
            {#if webhookInfo.secret}
              <button class="rounded-xl border border-slate-700 bg-slate-950/60 px-3 py-2 text-sm text-slate-100" onclick={() => webhookInfo?.secret && void copyWebhookInfo(webhookInfo.secret)} type="button">Copy secret</button>
            {/if}
            <button class="rounded-xl px-3 py-2 text-sm text-slate-300" onclick={() => { webhookInfoDismissed = true; }} type="button">Dismiss</button>
          </div>
        </div>
      </Card>
    {/if}

    <div class="flex flex-wrap gap-2">
      {#each [
        { id: 'accounts', label: `Accounts (${accounts.length})` },
        { id: 'pairing', label: `Pairing inbox (${pairingRequests.length})` },
        { id: 'contacts', label: `Verified senders (${contacts.length})` },
      ] as tab}
        <button
          class={`rounded-full px-4 py-2 text-sm font-medium transition ${activeTab === tab.id ? 'bg-sky-500 text-slate-950' : 'border border-slate-700 bg-slate-900 text-slate-300 hover:border-slate-500 hover:text-white'}`}
          onclick={() => handleTabChange(tab.id as ChannelsTab)}
          type="button"
        >
          {tab.label}
        </button>
      {/each}
    </div>

    {#if activeTab === 'accounts'}
      <div class="grid gap-6 lg:grid-cols-[1.15fr_0.85fr]">
        <div class={`${mobileEditorOpen ? 'hidden lg:block' : 'block'}`}>
          <ChannelAccountsView
            accounts={accounts}
            metas={metaMap}
            {busy}
            {selectedAccountId}
            agentName={(agentId) => getAgentName(agents, agentId)}
            pendingCount={accountPendingCount}
            onCreate={beginCreateDefault}
            onEdit={beginEdit}
            onToggle={(account) => void toggleAccount(account)}
            onDelete={(account) => void removeAccount(account)}
          />
        </div>

        <div class={`${mobileEditorOpen || editorMode !== 'closed' ? 'block' : 'hidden lg:block'}`}>
          {#if editorMode === 'closed'}
            <Card class="p-6 text-sm text-slate-300">
              <p class="text-xs uppercase tracking-[0.24em] text-slate-500">Account editor</p>
              <h2 class="mt-2 text-lg font-semibold text-white">Choose a platform or existing account</h2>
              <p class="mt-2">Select an account to edit, or start a new account setup to see platform-specific guidance here.</p>
              <button class="mt-4 rounded-xl bg-sky-500 px-4 py-2 text-sm font-medium text-slate-950" onclick={beginCreateDefault} type="button">Add channel</button>
            </Card>
          {:else}
            <ChannelAccountEditor
              mode={editorMode}
              {selectedType}
              {channelTypes}
              {draft}
              {credentialOverrides}
              {agents}
              {executors}
              guide={selectedGuide()}
              {busy}
              mobile={mobileEditorOpen}
              {isDirty}
              onClose={closeEditor}
              onSelectType={beginCreate}
              onSave={() => void (editorMode === 'edit' ? saveAccountChanges() : saveAccount())}
            />
          {/if}
        </div>
      </div>
    {:else if activeTab === 'pairing'}
      <PairingInboxView
        {pairingRequests}
        {redeemCode}
        {busy}
        onRedeem={() => void redeemPairingCode()}
        onRedeemCodeChange={(value) => { redeemCode = value; }}
        onApprove={(request) => {
          redeemCode = request.code;
          void redeemPairingCode();
        }}
        onReject={(requestId) => void rejectPairing(requestId)}
      />
    {:else}
      <ContactsView channelTypes={channelTypes} {contacts} {contactForm} {busy} onSave={() => void saveContact()} />
    {/if}
  </div>
{/if}
