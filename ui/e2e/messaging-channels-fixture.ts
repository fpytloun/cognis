import type { ChannelAccount, ChannelMeta } from '../src/lib/types/api';

export function channelMeta(channel_type: string, label: string): ChannelMeta {
  return {
    channel_type, label, description: `${label} messaging`, icon: channel_type,
    docs_url: null, connection_mode: 'long_poll',
    capabilities: {
      chat_types: ['direct'], supports_threads: false, supports_reactions: false,
      supports_edits: false, supports_media: false, supports_typing: false,
      supports_read_receipts: false, supports_markdown: true, supports_buttons: false,
      max_message_length: 4000,
    },
    credential_fields: [], setting_fields: [],
  };
}

export const channelTypes = [
  channelMeta('web', 'Cognis app'), channelMeta('signal', 'Signal'),
  channelMeta('whatsapp', 'WhatsApp'),
];

export function channelAccount(account_id: string, channel_type: string, agent_id: string): ChannelAccount {
  return {
    account_id, channel_type, agent_id, display_name: channel_type === 'web' ? 'Cognis app' : channel_type,
    enabled: true, config: {}, credential_refs: {}, allowed_senders: [],
    status: { status: 'connected' },
    dm_policy: 'pairing', group_policy: 'pairing', allow_new_conversations: true,
    preferred_for_task_delivery: channel_type === 'web',
    adapter_location: 'controller', created_at: null, updated_at: null,
  };
}

export const channelAccounts = [
  channelAccount('app-riker', 'web', 'riker'),
  channelAccount('signal-riker', 'signal', 'riker'),
  channelAccount('app-laforge', 'web', 'laforge'),
  channelAccount('whatsapp-riker', 'whatsapp', 'riker'),
];
