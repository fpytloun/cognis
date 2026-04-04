<script lang="ts">
  import { onMount } from 'svelte';
  import { CheckCircle2, Copy, Link2, MessagesSquare, PlugZap, RefreshCw, ShieldCheck, Trash2, XCircle } from 'lucide-svelte';

  import { api, asApiError } from '$lib/api/client';
  import LoadingState from '$lib/components/LoadingState.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import Card from '$lib/components/ui/Card.svelte';
  import Input from '$lib/components/ui/Input.svelte';
  import { confirmAction } from '$lib/stores/confirm';
  import { addToast } from '$lib/stores/toasts';
  import type { Agent, ChannelAccount, ChannelContact, ChannelMeta, PairingRequest } from '$lib/types/api';

  type ChannelsTab = 'accounts' | 'contacts' | 'pairing';
  type SetupGuide = {
    title: string;
    service: string;
    publicUrlNeeded: boolean;
    steps: string[];
  };

  const guides: Record<string, SetupGuide> = {
    signal: {
      title: 'Signal via signal-cli REST API',
      service: 'Run a local or remote signal-cli REST API first.',
      publicUrlNeeded: false,
      steps: [
        'Start signal-cli REST API, for example with Docker exposing port 8080.',
        'Link or register your Signal number through signal-cli, usually by scanning a QR code from the Signal mobile app.',
        'Paste the API URL and linked phone number below.'
      ]
    },
    whatsapp: {
      title: 'WhatsApp Business Cloud API',
      service: 'Meta hosts the API, but Cognis must be reachable from the public internet.',
      publicUrlNeeded: true,
      steps: [
        'Create a Meta developer app and add the WhatsApp product.',
        'Provision a business phone number and permanent access token.',
        'After saving this account, copy the webhook URL shown by Cognis into the Meta webhook settings.',
        'Use the generated webhook secret as the verify token in Meta.'
      ]
    },
    telegram: {
      title: 'Telegram Bot API',
      service: 'Telegram hosts the API.',
      publicUrlNeeded: false,
      steps: [
        'Open Telegram and chat with @BotFather.',
        'Create a bot with /newbot and copy the bot token.',
        'Long polling works immediately. If you prefer webhooks, enable them after saving and use the Cognis webhook URL.'
      ]
    },
    discord: {
      title: 'Discord bot setup',
      service: 'Discord hosts the API.',
      publicUrlNeeded: false,
      steps: [
        'Create an application in the Discord Developer Portal and add a bot user.',
        'Enable Message Content Intent so Cognis can read messages.',
        'Invite the bot to your server with bot permissions and paste the token below.',
        'Each Discord bot token should be used by one Cognis agent. The agent profile is synced to the bot globally.'
      ]
    },
    slack: {
      title: 'Slack app setup',
      service: 'Slack hosts the API.',
      publicUrlNeeded: false,
      steps: [
        'Create a Slack app and enable Socket Mode for the simplest setup.',
        'Generate an App-Level Token (xapp-...) and a Bot Token (xoxb-...).',
        'Add the chat:write.customize scope so the bot can display the agent name and avatar on messages.',
        'Subscribe to message events and install the app into your workspace.'
      ]
    },
    matrix: {
      title: 'Matrix homeserver access',
      service: 'Use any Matrix homeserver such as matrix.org or your own.',
      publicUrlNeeded: false,
      steps: [
        'Create or choose a Matrix account for the bot.',
        'Generate an access token by logging in or from an existing client session.',
        'Invite the bot account to the rooms it should join.'
      ]
    },
    irc: {
      title: 'IRC connection',
      service: 'Cognis connects directly to the IRC server.',
      publicUrlNeeded: false,
      steps: [
        'Choose the IRC server, port, nickname, and channels.',
        'If the network requires it, register the nickname with NickServ and provide the password.',
        'TLS is enabled by default.'
      ]
    },
    google_chat: {
      title: 'Google Chat bot',
      service: 'Requires Google Workspace and a public webhook URL.',
      publicUrlNeeded: true,
      steps: [
        'Create a Google Cloud project and enable the Google Chat API.',
        'Create a service account and paste the JSON credentials below.',
        'After saving, configure the Chat app HTTP endpoint to use the Cognis webhook URL.'
      ]
    }
  };

  const policyOptions = [
    { value: 'pairing', label: 'Pairing (recommended)' },
    { value: 'open', label: 'Open' },
    { value: 'allowlist', label: 'Allowlist' },
    { value: 'disabled', label: 'Disabled' }
  ];

  const accountStatusClass: Record<string, string> = {
    connected: 'bg-emerald-500/10 text-emerald-300 border border-emerald-500/20',
    connecting: 'bg-sky-500/10 text-sky-300 border border-sky-500/20',
    reconnecting: 'bg-amber-500/10 text-amber-300 border border-amber-500/20',
    error: 'bg-rose-500/10 text-rose-300 border border-rose-500/20',
    disconnected: 'bg-slate-800 text-slate-300 border border-slate-700',
    stopped: 'bg-slate-800 text-slate-300 border border-slate-700'
  };

  let loading = $state(true);
  let busy = $state(false);
  let error = $state('');
  let activeTab = $state<ChannelsTab>('accounts');

  let channelTypes = $state<ChannelMeta[]>([]);
  let accounts = $state<ChannelAccount[]>([]);
  let contacts = $state<ChannelContact[]>([]);
  let pairingRequests = $state<PairingRequest[]>([]);
  let agents = $state<Agent[]>([]);

  let selectedType = $state<ChannelMeta | null>(null);
  let showCreatePanel = $state(false);
  let webhookInfo = $state<{ url: string; secret: string | null; channelType: string } | null>(null);
  let webhookInfoDismissed = $state(false);

  let executors = $state<{ executor_id: string; name: string; status: string }[]>([]);
  let editingAccountId = $state<string | null>(null);
  let credentialOverrides = $state<Record<string, string>>({});

  let createForm = $state({
    display_name: '',
    agent_id: '',
    adapter_location: 'controller' as string,
    executor_id: '' as string,
    dm_policy: 'pairing',
    group_policy: 'pairing',
    allow_new_conversations: true,
    credentialValues: {} as Record<string, string>,
    settingValues: {} as Record<string, string>
  });

  let contactForm = $state({
    channel_type: 'signal',
    sender_id: '',
    display_name: ''
  });

  let redeemCode = $state('');

  function currentOrigin(): string {
    return typeof window === 'undefined' ? '' : window.location.origin;
  }

  function selectedGuide(): SetupGuide | null {
    return selectedType ? guides[selectedType.channel_type] ?? null : null;
  }

  function statusText(account: ChannelAccount): string {
    const value = account.status && 'status' in account.status ? account.status.status : 'stopped';
    return typeof value === 'string' ? value : 'stopped';
  }

  function statusClass(account: ChannelAccount): string {
    return accountStatusClass[statusText(account)] ?? accountStatusClass.stopped;
  }

  function agentName(agentId: string): string {
    return agents.find((agent) => agent.agent_id === agentId)?.name ?? agentId;
  }

  function formatRemaining(expiresAt: string): string {
    const remainingMs = new Date(expiresAt).getTime() - Date.now();
    if (remainingMs <= 0) {
      return 'expired';
    }
    const remainingMinutes = Math.ceil(remainingMs / 60_000);
    return `${remainingMinutes} min left`;
  }

  function normalizeSettingValue(meta: ChannelMeta, fieldName: string, value: string): string | boolean | number {
    const field = meta.setting_fields.find((item) => item.name === fieldName);
    if (!field) {
      return value;
    }
    if (field.field_type === 'boolean') {
      return value === 'true';
    }
    if (field.field_type === 'number') {
      const parsed = Number(value);
      return Number.isNaN(parsed) ? value : parsed;
    }
    return value;
  }

  function beginCreate(meta: ChannelMeta): void {
    editingAccountId = null;
    selectedType = meta;
    showCreatePanel = true;
    webhookInfo = null;
    webhookInfoDismissed = false;
    createForm.display_name = `${meta.label} Account`;
    createForm.agent_id = agents[0]?.agent_id ?? '';
    createForm.adapter_location = 'controller';
    createForm.executor_id = '';
    createForm.dm_policy = 'pairing';
    createForm.group_policy = 'pairing';
    createForm.allow_new_conversations = true;
    createForm.credentialValues = Object.fromEntries(meta.credential_fields.map((field) => [field.name, '']));
    createForm.settingValues = Object.fromEntries(
      meta.setting_fields.map((field) => [field.name, field.default == null ? '' : String(field.default)])
    );
    credentialOverrides = {};
  }

  function beginEdit(account: ChannelAccount): void {
    const meta = channelTypes.find((item) => item.channel_type === account.channel_type);
    if (!meta) {
      return;
    }
    editingAccountId = account.account_id;
    selectedType = meta;
    showCreatePanel = true;
    webhookInfo = null;
    webhookInfoDismissed = true;
    createForm.display_name = account.display_name;
    createForm.agent_id = account.agent_id;
    createForm.adapter_location = account.adapter_location ?? 'controller';
    createForm.executor_id = account.executor_id ?? '';
    createForm.dm_policy = account.dm_policy;
    createForm.group_policy = account.group_policy;
    createForm.allow_new_conversations = account.allow_new_conversations ?? true;
    createForm.credentialValues = Object.fromEntries(meta.credential_fields.map((field) => [field.name, '']));
    createForm.settingValues = Object.fromEntries(
      meta.setting_fields.map((field) => [field.name, String(account.config?.[field.name] ?? field.default ?? '')])
    );
    credentialOverrides = {};
    activeTab = 'accounts';
  }

  function cancelEdit(): void {
    editingAccountId = null;
    credentialOverrides = {};
    showCreatePanel = false;
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
        api.executor.list().catch(() => [])
      ]);
      channelTypes = types;
      accounts = accountsResult;
      contacts = contactsResult;
      pairingRequests = pairingResult;
      agents = agentsResult.filter((agent) => agent.status !== 'archived');
      executors = (executorsResult as { executor_id: string; name: string; status: string }[]) ?? [];
      if (!selectedType && types.length > 0) {
        beginCreate(types[0]);
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
      // Ignore background polling errors; the user still has the last snapshot.
    }
  }

  async function saveAccount(): Promise<void> {
    if (!selectedType) {
      return;
    }
    if (!createForm.agent_id) {
      addToast('Select an agent for this channel account.', 'error');
      return;
    }

    busy = true;
    error = '';
    const generatedSecretNames: string[] = [];
    try {
      const credential_refs: Record<string, string> = {};
      const settings: Record<string, unknown> = {};

      for (const field of selectedType.credential_fields) {
        const rawValue = (createForm.credentialValues[field.name] ?? '').trim();
        if (field.required && !rawValue) {
          throw new Error(`${field.label} is required.`);
        }
        if (!rawValue) {
          continue;
        }
        if (field.secret) {
          const secretName = `channel.${selectedType.channel_type}.${Date.now()}.${field.name}`;
          generatedSecretNames.push(secretName);
          await api.secrets.upsert({
            name: secretName,
            value: rawValue,
            scope: 'user',
            agent_id: null,
            description: `${selectedType.label} credential: ${field.label}`
          });
          credential_refs[field.name] = secretName;
        } else {
          settings[field.name] = rawValue;
        }
      }

      for (const field of selectedType.setting_fields) {
        const rawValue = createForm.settingValues[field.name] ?? '';
        if (rawValue === '') {
          continue;
        }
        settings[field.name] = normalizeSettingValue(selectedType, field.name, rawValue);
      }

      const created = await api.channels.createAccount({
        channel_type: selectedType.channel_type,
        display_name: createForm.display_name,
        agent_id: createForm.agent_id,
        settings,
        credential_refs,
        adapter_location: createForm.adapter_location,
        executor_id: createForm.executor_id || null,
        dm_policy: createForm.dm_policy,
        group_policy: createForm.group_policy,
        allow_new_conversations: createForm.allow_new_conversations
      });

      webhookInfo = selectedGuide()?.publicUrlNeeded
        ? {
            channelType: created.channel_type,
            url: `${currentOrigin()}/api/v1/channels/webhook/${created.channel_type}/${created.account_id}`,
            secret: created.webhook_secret
          }
        : null;
      webhookInfoDismissed = false;

      addToast('Channel account created.', 'success');
      await loadData();
    } catch (caughtError) {
      const message = asApiError(caughtError).message;
      error = message;
      addToast(message, 'error', 4_000, 'Unable to create channel account');
      for (const secretName of generatedSecretNames) {
        try {
          await api.secrets.remove(secretName, 'user', null);
        } catch {
          // Best effort cleanup of generated secrets on failure.
        }
      }
    } finally {
      busy = false;
    }
  }

  async function saveAccountChanges(): Promise<void> {
    if (!selectedType || !editingAccountId) {
      return;
    }
    busy = true;
    error = '';
    try {
      const current = accounts.find((account) => account.account_id === editingAccountId);
      if (!current) {
        throw new Error('Channel account no longer exists.');
      }

      const updates: Record<string, unknown> = {
        display_name: createForm.display_name,
        agent_id: createForm.agent_id,
        adapter_location: createForm.adapter_location,
        executor_id: createForm.executor_id || null,
        dm_policy: createForm.dm_policy,
        group_policy: createForm.group_policy,
        allow_new_conversations: createForm.allow_new_conversations,
      };

      const config: Record<string, unknown> = {};
      const replacedSecretNames: string[] = [];
      for (const field of selectedType.setting_fields) {
        const rawValue = createForm.settingValues[field.name] ?? '';
        if (rawValue === '') {
          continue;
        }
        config[field.name] = normalizeSettingValue(selectedType, field.name, rawValue);
      }
      updates.config = config;

      const credential_refs: Record<string, string> = { ...(current.credential_refs ?? {}) };
      for (const field of selectedType.credential_fields) {
        const replacement = (credentialOverrides[field.name] ?? '').trim();
        if (!replacement) {
          continue;
        }
        if (field.secret) {
          const secretName = `channel.${selectedType.channel_type}.${Date.now()}.${field.name}`;
          await api.secrets.upsert({
            name: secretName,
            value: replacement,
            scope: 'user',
            agent_id: null,
            description: `${selectedType.label} credential: ${field.label}`
          });
          const previousSecret = credential_refs[field.name];
          if (previousSecret && previousSecret !== secretName) {
            replacedSecretNames.push(previousSecret);
          }
          credential_refs[field.name] = secretName;
        } else {
          (updates.config as Record<string, unknown>)[field.name] = replacement;
        }
      }
      updates.credential_refs = credential_refs;

      await api.channels.updateAccount(editingAccountId, updates);
      for (const secretName of replacedSecretNames) {
        try {
          await api.secrets.remove(secretName, 'user', null);
        } catch {
          // Best effort cleanup for rotated secrets.
        }
      }
      addToast('Channel account updated.', 'success');
      editingAccountId = null;
      credentialOverrides = {};
      await loadData();
    } catch (caughtError) {
      addToast(asApiError(caughtError).message, 'error', 4_000, 'Unable to update channel account');
    } finally {
      busy = false;
    }
  }

  async function toggleAccount(account: ChannelAccount): Promise<void> {
    busy = true;
    try {
      if (statusText(account) === 'connected') {
        await api.channels.stopAccount(account.account_id);
        addToast('Channel stopped.', 'success');
      } else {
        await api.channels.startAccount(account.account_id);
        addToast('Channel start requested.', 'success');
      }
      accounts = await api.channels.listAccounts();
    } catch (caughtError) {
      addToast(asApiError(caughtError).message, 'error', 4_000, 'Unable to change channel status');
    } finally {
      busy = false;
    }
  }

  async function removeAccount(account: ChannelAccount): Promise<void> {
    const confirmed = await confirmAction({
      title: 'Delete channel account?',
      message: `This removes ${account.display_name} and stops its adapter.`,
      confirmLabel: 'Delete account'
    });
    if (!confirmed) {
      return;
    }
    busy = true;
    try {
      await api.channels.deleteAccount(account.account_id);
      addToast('Channel account deleted.', 'success');
      accounts = await api.channels.listAccounts();
    } catch (caughtError) {
      addToast(asApiError(caughtError).message, 'error', 4_000, 'Unable to delete account');
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
        verified: true
      });
      contactForm.sender_id = '';
      contactForm.display_name = '';
      contacts = await api.channels.listContacts();
      addToast('Verified contact saved.', 'success');
    } catch (caughtError) {
      addToast(asApiError(caughtError).message, 'error', 4_000, 'Unable to save contact');
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
      addToast(asApiError(caughtError).message, 'error', 4_000, 'Unable to redeem pairing code');
    } finally {
      busy = false;
    }
  }

  async function rejectPairing(requestId: string): Promise<void> {
    const confirmed = await confirmAction({
      title: 'Reject pairing request?',
      message: 'The remote sender will stay blocked until they request a new code.',
      confirmLabel: 'Reject request'
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
      addToast(asApiError(caughtError).message, 'error', 4_000, 'Unable to reject pairing request');
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
      window.clearInterval(interval);
    };
  });
</script>

{#if loading}
  <LoadingState label="Loading channels" description="Fetching channel accounts, contacts, and pairing requests." />
{:else}
  <div class="space-y-6">
    <div class="flex flex-col gap-3 lg:flex-row lg:items-end lg:justify-between">
      <div>
        <p class="text-xs uppercase tracking-[0.28em] text-sky-300/80">Channels</p>
        <h1 class="mt-2 text-3xl font-semibold text-white">External messaging connections</h1>
        <p class="mt-2 max-w-3xl text-sm text-slate-300">
          Connect agents to chat platforms, verify remote senders with pairing codes, and keep onboarding simple with adapter-specific setup guidance.
        </p>
      </div>
      <div class="flex gap-2">
        <Button variant="secondary" onclick={() => void loadData()} disabled={busy}>
          <RefreshCw class="mr-2 h-4 w-4" /> Refresh
        </Button>
        <Button variant="primary" onclick={() => { activeTab = 'accounts'; showCreatePanel = true; }}>
          <PlugZap class="mr-2 h-4 w-4" /> Add channel
        </Button>
      </div>
    </div>

    {#if error}
      <Card class="border border-rose-500/20 bg-rose-500/10 p-4 text-sm text-rose-200">{error}</Card>
    {/if}

    {#if webhookInfo && !webhookInfoDismissed}
      <Card class="border border-emerald-500/20 bg-emerald-500/10 p-5 text-sm text-emerald-100">
        <div class="flex items-start justify-between gap-4">
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
            <Button variant="secondary" size="sm" onclick={() => webhookInfo && void copyWebhookInfo(webhookInfo.url)}>
              <Copy class="mr-2 h-4 w-4" /> Copy URL
            </Button>
            {#if webhookInfo.secret}
              <Button variant="secondary" size="sm" onclick={() => webhookInfo?.secret && void copyWebhookInfo(webhookInfo.secret)}>
                <Copy class="mr-2 h-4 w-4" /> Copy secret
              </Button>
            {/if}
            <Button variant="ghost" size="sm" onclick={() => { webhookInfoDismissed = true; }}>
              Dismiss
            </Button>
          </div>
        </div>
      </Card>
    {/if}

    <div class="flex flex-wrap gap-2">
      {#each [
        { id: 'accounts', label: `Accounts (${accounts.length})` },
        { id: 'contacts', label: `Contacts (${contacts.length})` },
        { id: 'pairing', label: `Pairing (${pairingRequests.length})` }
      ] as tab}
        <button
          class={`rounded-full px-4 py-2 text-sm font-medium transition ${activeTab === tab.id ? 'bg-sky-500 text-slate-950' : 'border border-slate-700 bg-slate-900 text-slate-300 hover:border-slate-500 hover:text-white'}`}
          onclick={() => {
            activeTab = tab.id as ChannelsTab;
            if (tab.id === 'pairing') {
              void refreshPairingRequests();
            }
          }}
        >
          {tab.label}
        </button>
      {/each}
    </div>

    {#if activeTab === 'accounts'}
      <div class="grid gap-6 xl:grid-cols-[1.2fr_0.8fr]">
        <div class="space-y-4">
          {#if accounts.length === 0}
            <Card class="p-6 text-sm text-slate-300">
              No channel accounts yet. Start by choosing a platform on the right and follow the setup guide.
            </Card>
          {/if}

          {#each accounts as account}
            <Card class="p-5">
              <div class="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
                <div class="space-y-2">
                  <div class="flex flex-wrap items-center gap-2">
                    <h2 class="text-lg font-semibold text-white">{account.display_name}</h2>
                    <span class={`rounded-full px-3 py-1 text-xs font-medium ${statusClass(account)}`}>{statusText(account)}</span>
                    <span class="rounded-full border border-slate-700 px-3 py-1 text-xs text-slate-300">{account.channel_type}</span>
                  </div>
                  <p class="text-sm text-slate-300">Agent: {agentName(account.agent_id)}</p>
                  <div class="flex flex-wrap gap-2 text-xs text-slate-400">
                    {#if account.adapter_location === 'executor'}
                      <span class="rounded-full border border-violet-500/20 bg-violet-500/10 px-3 py-1 text-violet-300">executor</span>
                    {/if}
                    <span class="rounded-full border border-slate-700 px-3 py-1">DM: {account.dm_policy}</span>
                    <span class="rounded-full border border-slate-700 px-3 py-1">Groups: {account.group_policy}</span>
                    <span class="rounded-full border border-slate-700 px-3 py-1">New conversations: {account.allow_new_conversations ? 'yes' : 'no'}</span>
                  </div>
                  {#if account.status && 'last_error' in account.status && account.status.last_error}
                    <p class="text-sm text-rose-300">Last error: {account.status.last_error}</p>
                  {/if}
                </div>

                <div class="flex flex-wrap gap-2">
                  <Button variant="secondary" size="sm" onclick={() => void toggleAccount(account)} disabled={busy}>
                    {#if statusText(account) === 'connected'}
                      <XCircle class="mr-2 h-4 w-4" /> Stop
                    {:else}
                      <CheckCircle2 class="mr-2 h-4 w-4" /> Start
                    {/if}
                  </Button>
                  <Button variant="secondary" size="sm" onclick={() => beginEdit(account)} disabled={busy}>
                    Edit
                  </Button>
                  <Button variant="danger" size="sm" onclick={() => void removeAccount(account)} disabled={busy}>
                    <Trash2 class="mr-2 h-4 w-4" /> Delete
                  </Button>
                </div>
              </div>
            </Card>
          {/each}
        </div>

        <div class="space-y-4">
          <Card class="p-5">
            <div class="flex items-center justify-between gap-3">
              <div>
                <h2 class="text-lg font-semibold text-white">{editingAccountId ? 'Edit channel account' : 'Create channel account'}</h2>
                <p class="mt-1 text-sm text-slate-400">{editingAccountId ? 'Update settings, policies, executor placement, or replace stored credentials.' : 'Choose the adapter first, then follow the platform-specific setup steps.'}</p>
              </div>
            </div>

            {#if !editingAccountId}
              <div class="mt-5 grid gap-3 sm:grid-cols-2">
                {#each channelTypes as meta}
                  <button
                    class={`rounded-2xl border p-4 text-left transition ${selectedType?.channel_type === meta.channel_type ? 'border-sky-400 bg-sky-500/10' : 'border-slate-700 bg-slate-950/60 hover:border-slate-500'}`}
                    onclick={() => beginCreate(meta)}
                  >
                    <div class="flex items-center gap-2 text-white">
                      <MessagesSquare class="h-4 w-4 text-sky-300" />
                      <span class="font-medium">{meta.label}</span>
                    </div>
                    <p class="mt-2 text-sm text-slate-400">{meta.description}</p>
                  </button>
                {/each}
              </div>
            {/if}
          </Card>

          {#if showCreatePanel && selectedType}
            <Card class="p-5">
              <div class="flex items-start justify-between gap-3">
                <div>
                  <h3 class="text-lg font-semibold text-white">{selectedGuide()?.title ?? selectedType.label}</h3>
                  <p class="mt-1 text-sm text-slate-400">{selectedGuide()?.service ?? selectedType.description}</p>
                </div>
                {#if selectedType.docs_url}
                  <a class="inline-flex items-center gap-2 text-sm text-sky-300 hover:text-sky-200" href={selectedType.docs_url} target="_blank" rel="noreferrer">
                    <Link2 class="h-4 w-4" /> Docs
                  </a>
                {/if}
              </div>

              <div class="mt-4 rounded-2xl border border-slate-800 bg-slate-950/60 p-4">
                <p class="text-xs uppercase tracking-[0.24em] text-slate-500">Manual setup</p>
                <ol class="mt-3 space-y-2 text-sm text-slate-300">
                  {#each selectedGuide()?.steps ?? [] as step, index}
                    <li class="flex gap-3"><span class="text-sky-300">{index + 1}.</span><span>{step}</span></li>
                  {/each}
                </ol>
                <p class="mt-3 text-xs text-slate-500">
                  {#if selectedGuide()?.publicUrlNeeded}
                    This adapter needs a public webhook URL. Save the account first, then copy the generated webhook URL from Cognis.
                  {:else}
                    This adapter does not require a public webhook URL for the default setup.
                  {/if}
                </p>
              </div>

              <div class="mt-5 grid gap-4">
                <label class="grid gap-2 text-sm text-slate-300">
                  Display name
                  <Input bind:value={createForm.display_name} placeholder={`${selectedType.label} Account`} />
                </label>

                <label class="grid gap-2 text-sm text-slate-300">
                  Agent
                  <select bind:value={createForm.agent_id} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100">
                    <option value="">Select an agent</option>
                    {#each agents as agent}
                      <option value={agent.agent_id}>{agent.name}</option>
                    {/each}
                  </select>
                </label>

                <label class="grid gap-2 text-sm text-slate-300">
                  Adapter location
                  <select bind:value={createForm.adapter_location} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100">
                    <option value="controller">Controller (default)</option>
                    <option value="executor">Executor (remote)</option>
                  </select>
                  <span class="text-xs text-slate-500">
                    {#if createForm.adapter_location === 'executor'}
                      The adapter will run on the selected executor. Use this for platforms that need user-local services (e.g. Signal via signal-cli).
                    {:else}
                      The adapter runs on the Cognis controller. Best for cloud APIs and webhook-based platforms.
                    {/if}
                  </span>
                </label>

                {#if createForm.adapter_location === 'executor'}
                  <label class="grid gap-2 text-sm text-slate-300">
                    Executor
                    <select bind:value={createForm.executor_id} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100">
                      <option value="">Any connected executor</option>
                      {#each executors as executor}
                        <option value={executor.executor_id}>{executor.name} ({executor.status})</option>
                      {/each}
                    </select>
                    <span class="text-xs text-slate-500">The executor must be connected and running when the channel starts.</span>
                  </label>
                {/if}

                {#each selectedType.credential_fields as field}
                  <label class="grid gap-2 text-sm text-slate-300">
                    {field.label}
                    <Input
                      value={editingAccountId ? (credentialOverrides[field.name] ?? '') : (createForm.credentialValues[field.name] ?? '')}
                      oninput={(event) => {
                        const value = (event.currentTarget as HTMLInputElement).value;
                        if (editingAccountId) {
                          credentialOverrides[field.name] = value;
                        } else {
                          createForm.credentialValues[field.name] = value;
                        }
                      }}
                      type={field.secret ? 'password' : 'text'}
                      placeholder={editingAccountId ? 'Configured (enter new value to replace)' : (field.description || field.label)}
                    />
                    {#if field.description}
                      <span class="text-xs text-slate-500">{field.description}</span>
                    {/if}
                  </label>
                {/each}

                {#each selectedType.setting_fields as field}
                  <label class="grid gap-2 text-sm text-slate-300">
                    {field.label}
                    {#if field.field_type === 'select' && field.options}
                      <select bind:value={createForm.settingValues[field.name]} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100">
                        {#each field.options as option}
                          <option value={option}>{option}</option>
                        {/each}
                      </select>
                    {:else if field.field_type === 'boolean'}
                      <select bind:value={createForm.settingValues[field.name]} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100">
                        <option value="true">true</option>
                        <option value="false">false</option>
                      </select>
                    {:else}
                      <Input bind:value={createForm.settingValues[field.name]} placeholder={field.description || field.label} />
                    {/if}
                    {#if field.description}
                      <span class="text-xs text-slate-500">{field.description}</span>
                    {/if}
                  </label>
                {/each}

                <div class="grid gap-4 md:grid-cols-2">
                  <label class="grid gap-2 text-sm text-slate-300">
                    DM policy
                    <select bind:value={createForm.dm_policy} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100">
                      {#each policyOptions as option}
                        <option value={option.value}>{option.label}</option>
                      {/each}
                    </select>
                  </label>

                  <label class="grid gap-2 text-sm text-slate-300">
                    Group policy
                    <select bind:value={createForm.group_policy} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100">
                      {#each policyOptions as option}
                        <option value={option.value}>{option.label}</option>
                      {/each}
                    </select>
                  </label>
                </div>

                <label class="flex items-center gap-3 rounded-2xl border border-slate-800 bg-slate-950/60 px-4 py-3 text-sm text-slate-300">
                  <input bind:checked={createForm.allow_new_conversations} type="checkbox" class="h-4 w-4 rounded border-slate-600 bg-slate-900" />
                  Allow this adapter to create new conversations automatically when a new chat appears.
                </label>

                <div class="flex justify-end gap-2">
                  {#if editingAccountId}
                    <Button variant="secondary" onclick={cancelEdit} disabled={busy}>Cancel</Button>
                    <Button variant="primary" onclick={() => void saveAccountChanges()} disabled={busy}>
                      Save changes
                    </Button>
                  {:else}
                    <Button variant="primary" onclick={() => void saveAccount()} disabled={busy}>
                      <PlugZap class="mr-2 h-4 w-4" /> Save channel account
                    </Button>
                  {/if}
                </div>
              </div>
            </Card>
          {/if}
        </div>
      </div>
    {:else if activeTab === 'contacts'}
      <div class="grid gap-6 xl:grid-cols-[0.85fr_1.15fr]">
        <Card class="p-5">
          <h2 class="text-lg font-semibold text-white">Add verified contact</h2>
          <p class="mt-1 text-sm text-slate-400">Use this when you already know the external sender ID and want to skip the pairing flow.</p>

          <div class="mt-5 grid gap-4">
            <label class="grid gap-2 text-sm text-slate-300">
              Channel type
              <select bind:value={contactForm.channel_type} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100">
                {#each channelTypes as meta}
                  <option value={meta.channel_type}>{meta.label}</option>
                {/each}
              </select>
            </label>
            <label class="grid gap-2 text-sm text-slate-300">
              Sender ID
              <Input bind:value={contactForm.sender_id} placeholder="Phone number, platform user ID, chat user handle, ..." />
            </label>
            <label class="grid gap-2 text-sm text-slate-300">
              Display name
              <Input bind:value={contactForm.display_name} placeholder="Optional friendly label" />
            </label>
            <div class="flex justify-end">
              <Button variant="primary" onclick={() => void saveContact()} disabled={busy}>
                <ShieldCheck class="mr-2 h-4 w-4" /> Save contact
              </Button>
            </div>
          </div>
        </Card>

        <div class="space-y-4">
          {#if contacts.length === 0}
            <Card class="p-6 text-sm text-slate-300">No verified contacts yet. Pair a remote sender or add one manually.</Card>
          {/if}
          {#each contacts as contact}
            <Card class="p-5">
              <div class="flex flex-col gap-2 lg:flex-row lg:items-center lg:justify-between">
                <div>
                  <div class="flex flex-wrap items-center gap-2">
                    <h2 class="text-lg font-semibold text-white">{contact.display_name || contact.sender_id}</h2>
                    <span class="rounded-full border border-emerald-500/20 bg-emerald-500/10 px-3 py-1 text-xs text-emerald-300">verified</span>
                    <span class="rounded-full border border-slate-700 px-3 py-1 text-xs text-slate-300">{contact.channel_type}</span>
                  </div>
                  <p class="mt-2 text-sm text-slate-400">Sender ID: {contact.sender_id}</p>
                  <p class="text-sm text-slate-400">Linked to your Cognis account</p>
                </div>
              </div>
            </Card>
          {/each}
        </div>
      </div>
    {:else}
      <div class="grid gap-6 xl:grid-cols-[0.8fr_1.2fr]">
        <Card class="p-5">
          <h2 class="text-lg font-semibold text-white">Redeem pairing code</h2>
          <p class="mt-1 text-sm text-slate-400">When an unknown sender messages your agent, they receive a short-lived verification code. Enter it here to approve them.</p>

          <div class="mt-5 grid gap-4">
            <label class="grid gap-2 text-sm text-slate-300">
              Pairing code
              <Input bind:value={redeemCode} placeholder="ABC-123" />
            </label>
            <div class="flex justify-end">
              <Button variant="primary" onclick={() => void redeemPairingCode()} disabled={busy}>
                <CheckCircle2 class="mr-2 h-4 w-4" /> Redeem code
              </Button>
            </div>
          </div>
        </Card>

        <div class="space-y-4">
          {#if pairingRequests.length === 0}
            <Card class="p-6 text-sm text-slate-300">No pending pairing requests. When someone new messages a paired channel account, their request will appear here.</Card>
          {/if}
          {#each pairingRequests as request}
            <Card class="p-5">
              <div class="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
                <div>
                  <div class="flex flex-wrap items-center gap-2">
                    <h2 class="text-lg font-semibold text-white">{request.sender_name || request.sender_id}</h2>
                    <span class="rounded-full border border-slate-700 px-3 py-1 text-xs text-slate-300">{request.channel_type}</span>
                    <span class="rounded-full border border-amber-500/20 bg-amber-500/10 px-3 py-1 text-xs text-amber-300">{formatRemaining(request.expires_at)}</span>
                  </div>
                  <p class="mt-2 text-sm text-slate-400">Sender ID: {request.sender_id}</p>
                  <p class="text-sm text-slate-400">Chat: {request.chat_name || request.chat_id}</p>
                  <code class="mt-3 inline-block rounded-xl bg-slate-950/80 px-3 py-2 text-sm text-slate-100">{request.code}</code>
                </div>
                <div class="flex gap-2">
                  <Button variant="secondary" size="sm" onclick={() => { redeemCode = request.code; void redeemPairingCode(); }} disabled={busy}>
                    <CheckCircle2 class="mr-2 h-4 w-4" /> Approve
                  </Button>
                  <Button variant="danger" size="sm" onclick={() => void rejectPairing(request.request_id)} disabled={busy}>
                    <XCircle class="mr-2 h-4 w-4" /> Reject
                  </Button>
                </div>
              </div>
            </Card>
          {/each}
        </div>
      </div>
    {/if}
  </div>
{/if}
