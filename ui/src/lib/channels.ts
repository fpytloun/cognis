import type { Agent, ChannelAccount, ChannelContact, ChannelMeta, PairingRequest } from '$lib/types/api';

export type ChannelsTab = 'accounts' | 'pairing' | 'contacts';
export type ChannelEditorMode = 'closed' | 'create' | 'edit';

export interface ChannelEditorDraft {
  display_name: string;
  agent_id: string;
  adapter_location: string;
  executor_id: string;
  dm_policy: string;
  group_policy: string;
  allow_new_conversations: boolean;
  preferred_for_task_delivery: boolean;
  credentialValues: Record<string, string>;
  settingValues: Record<string, string>;
}

export interface SetupGuide {
  title: string;
  service: string;
  publicUrlNeeded: boolean;
  steps: string[];
}

export interface SummaryCardItem {
  id: string;
  label: string;
  value: string;
  detail: string;
}

export const guides: Record<string, SetupGuide> = {
  signal: {
    title: 'Signal via signal-cli',
    service: 'Two transport modes: REST API (external signal-cli-rest-api) or direct JSON-RPC (executor-managed signal-cli).',
    publicUrlNeeded: false,
    steps: [
      'REST API mode: Start signal-cli REST API (e.g. via Docker), link your number, and paste the API URL and phone number below.',
      'Direct JSON-RPC mode: Set transport to "direct_jsonrpc", assign an executor with signal.direct_enabled=true in its config, and provide the linked phone number. If signal-cli is not on PATH, set signal.command on that executor.',
      'For both modes, link or register your Signal number through signal-cli first (e.g. by scanning a QR code from the Signal mobile app).',
    ],
  },
  whatsapp: {
    title: 'WhatsApp Business Cloud API',
    service: 'Meta hosts the API, but Cognis must be reachable from the public internet.',
    publicUrlNeeded: true,
    steps: [
      'Create a Meta developer app and add the WhatsApp product.',
      'Provision a business phone number and permanent access token.',
      'After saving this account, copy the webhook URL shown by Cognis into the Meta webhook settings.',
      'Use the generated webhook secret as the verify token in Meta.',
    ],
  },
  telegram: {
    title: 'Telegram Bot API',
    service: 'Telegram hosts the API.',
    publicUrlNeeded: false,
    steps: [
      'Open Telegram and chat with @BotFather.',
      'Create a bot with /newbot and copy the bot token.',
      'Long polling works immediately. If you prefer webhooks, enable them after saving and use the Cognis webhook URL.',
    ],
  },
  discord: {
    title: 'Discord bot setup',
    service: 'Discord hosts the API.',
    publicUrlNeeded: false,
    steps: [
      'Create an application in the Discord Developer Portal and add a bot user.',
      'Enable Message Content Intent so Cognis can read messages.',
      'Invite the bot to your server with bot permissions and paste the token below.',
      'Each Discord bot token should be used by one Cognis agent. The agent profile is synced to the bot globally.',
    ],
  },
  slack: {
    title: 'Slack app setup',
    service: 'Slack hosts the API.',
    publicUrlNeeded: false,
    steps: [
      'Create a Slack app and enable Socket Mode for the simplest setup.',
      'Generate an App-Level Token (xapp-...) and a Bot Token (xoxb-...).',
      'Add the chat:write.customize scope so the bot can display the agent name and avatar on messages.',
      'Subscribe to message events and install the app into your workspace.',
    ],
  },
  matrix: {
    title: 'Matrix homeserver access',
    service: 'Use any Matrix homeserver such as matrix.org or your own.',
    publicUrlNeeded: false,
    steps: [
      'Create or choose a Matrix account for the bot.',
      'Generate an access token by logging in or from an existing client session.',
      'Invite the bot account to the rooms it should join.',
    ],
  },
  irc: {
    title: 'IRC connection',
    service: 'Cognis connects directly to the IRC server.',
    publicUrlNeeded: false,
    steps: [
      'Choose the IRC server, port, nickname, and channels.',
      'If the network requires it, register the nickname with NickServ and provide the password.',
      'TLS is enabled by default.',
    ],
  },
  google_chat: {
    title: 'Google Chat bot',
    service: 'Requires Google Workspace and a public webhook URL.',
    publicUrlNeeded: true,
    steps: [
      'Create a Google Cloud project and enable the Google Chat API.',
      'Create a service account and paste the JSON credentials below.',
      'After saving, configure the Chat app HTTP endpoint to use the Cognis webhook URL.',
    ],
  },
  bluebubbles: {
    title: 'iMessage via BlueBubbles',
    service: 'Requires a macOS machine running the BlueBubbles server app.',
    publicUrlNeeded: true,
    steps: [
      'Install the BlueBubbles server on a macOS machine (bluebubbles.app/install).',
      'In BlueBubbles settings, enable the web API and set an API password.',
      'Make the BlueBubbles server reachable from Cognis (e.g. via Ngrok, Cloudflare tunnel, or LAN).',
      'Enter the BlueBubbles server URL and API password below.',
      'After saving, copy the Cognis webhook URL and register it in BlueBubbles (Settings → Webhooks → Add).',
      'Use the same API password as the webhook password so BlueBubbles authenticates webhook requests.',
    ],
  },
};

export const policyOptions = [
  { value: 'pairing', label: 'Pairing (recommended)' },
  { value: 'open', label: 'Open' },
  { value: 'allowlist', label: 'Allowlist' },
  { value: 'disabled', label: 'Disabled' },
];

const accountStatusClass: Record<string, string> = {
  connected: 'bg-emerald-500/10 text-emerald-300 border border-emerald-500/20',
  connecting: 'bg-sky-500/10 text-sky-300 border border-sky-500/20',
  reconnecting: 'bg-sky-500/10 text-sky-300 border border-sky-500/20',
  error: 'bg-rose-500/10 text-rose-300 border border-rose-500/20',
  disconnected: 'bg-slate-800 text-slate-300 border border-slate-700',
  stopped: 'bg-slate-800 text-slate-300 border border-slate-700',
};

export function getChannelGuide(channelType: string | null | undefined): SetupGuide | null {
  return channelType ? guides[channelType] ?? null : null;
}

export function statusText(account: ChannelAccount): string {
  const value = account.status && 'status' in account.status ? account.status.status : 'stopped';
  return typeof value === 'string' ? value : 'stopped';
}

export function statusClass(account: ChannelAccount): string {
  return accountStatusClass[statusText(account)] ?? accountStatusClass.stopped;
}

export function formatRemaining(expiresAt: string): string {
  const remainingMs = new Date(expiresAt).getTime() - Date.now();
  if (remainingMs <= 0) {
    return 'expired';
  }
  return `${Math.ceil(remainingMs / 60_000)} min left`;
}

export function normalizeSettingValue(meta: ChannelMeta, fieldName: string, value: string): string | boolean | number {
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

export function createChannelDraft(meta: ChannelMeta, agents: Agent[], account?: ChannelAccount | null): ChannelEditorDraft {
  const primaryAgents = agents.filter((agent) => agent.agent_type === 'primary');
  const credentialValues = Object.fromEntries(
    meta.credential_fields.map((field) => {
      const configured = account?.config?.[field.name];
      const value = field.secret ? '' : String(configured ?? '');
      return [field.name, value];
    }),
  );
  return {
    display_name: account?.display_name ?? `${meta.label} Account`,
    agent_id: account?.agent_id ?? primaryAgents[0]?.agent_id ?? '',
    adapter_location: account?.adapter_location ?? 'controller',
    executor_id: account?.executor_id ?? '',
    dm_policy: account?.dm_policy ?? 'pairing',
    group_policy: account?.group_policy ?? 'pairing',
    allow_new_conversations: account?.allow_new_conversations ?? true,
    preferred_for_task_delivery: account?.preferred_for_task_delivery ?? false,
    credentialValues,
    settingValues: Object.fromEntries(
      meta.setting_fields.map((field) => [field.name, String(account?.config?.[field.name] ?? field.default ?? '')]),
    ),
  };
}

export function getPendingPairingCount(accountId: string, pairingRequests: PairingRequest[]): number {
  return pairingRequests.filter((request) => request.account_id === accountId && request.status === 'pending').length;
}

export function buildSummaryCards(
  accounts: ChannelAccount[],
  contacts: ChannelContact[],
  pairingRequests: PairingRequest[],
  ready: boolean,
): SummaryCardItem[] {
  if (!ready) {
    return [
      { id: 'accounts', label: 'Accounts', value: '--', detail: 'Loading account status' },
      { id: 'connected', label: 'Connected', value: '--', detail: 'Checking active adapters' },
      { id: 'pairing', label: 'Pending pairing', value: '--', detail: 'Loading sender approvals' },
      { id: 'contacts', label: 'Verified senders', value: '--', detail: 'Loading trusted senders' },
    ];
  }

  const connected = accounts.filter((account) => statusText(account) === 'connected').length;
  const pendingPairing = pairingRequests.filter((request) => request.status === 'pending').length;
  return [
    { id: 'accounts', label: 'Accounts', value: String(accounts.length), detail: 'Connected adapters and webhook accounts' },
    { id: 'connected', label: 'Connected', value: String(connected), detail: 'Adapters currently online' },
    { id: 'pairing', label: 'Pending pairing', value: String(pendingPairing), detail: 'Remote senders waiting for approval' },
    { id: 'contacts', label: 'Verified senders', value: String(contacts.length), detail: 'Manual or paired trusted contacts' },
  ];
}

export function getChannelMetaByType(channelTypes: ChannelMeta[], channelType: string): ChannelMeta | null {
  return channelTypes.find((item) => item.channel_type === channelType) ?? null;
}

export function getAgentName(agents: Agent[], agentId: string | null | undefined): string {
  if (!agentId) {
    return 'Unknown agent';
  }
  return agents.find((agent) => agent.agent_id === agentId)?.display_name
    ?? agents.find((agent) => agent.agent_id === agentId)?.name
    ?? agentId;
}
