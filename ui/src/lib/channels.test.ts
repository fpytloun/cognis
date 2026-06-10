import { describe, expect, it } from 'vitest';

import {
  buildSummaryCards,
  createChannelDraft,
  formatRemaining,
  getPendingPairingCount,
  normalizeSettingValue,
} from '$lib/channels';
import type { Agent, ChannelAccount, ChannelContact, ChannelMeta, PairingRequest } from '$lib/types/api';

const meta: ChannelMeta = {
  channel_type: 'signal',
  label: 'Signal',
  description: 'Signal channel',
  icon: 'signal',
  docs_url: null,
  connection_mode: 'long_poll',
  capabilities: {
    chat_types: ['direct'],
    supports_threads: false,
    supports_reactions: false,
    supports_edits: false,
    supports_media: true,
    supports_typing: false,
    supports_read_receipts: false,
    supports_markdown: false,
    supports_buttons: false,
    max_message_length: 4000,
  },
  credential_fields: [{ name: 'token', label: 'Token', description: '', required: true, secret: true }],
  setting_fields: [{ name: 'use_tls', label: 'Use TLS', description: '', field_type: 'boolean', default: true }],
};

const agent: Agent = {
  agent_id: 'agent-1', owner_email: 'user@example.com', name: 'Agent', display_name: 'Agent', description: null,
  system_prompt: null, personality: null, skills: null, tools: null, permissions: null, llm_config: null,
  execution: null, personality_synced: false, personality_sync_error: null, personality_sync_checked_at: null,
  avatar_url: null, avatar_image_id: null, agent_type: 'primary', is_system: false, hidden: false,
  editable_fields: [], has_overrides: false, disabled: false, disableable: false, sync_metadata: null,
  is_shared_with_me: false, shared_by_email: null, granted_permission: null, executor_scope: null,
  is_readonly_for_caller: false, status: 'active',
  created_at: null, updated_at: null,
};

describe('channels helpers', () => {
  it('creates a canonical draft for create and edit flows', () => {
    const created = createChannelDraft(meta, [agent]);
    expect(created.display_name).toBe('Signal Account');
    expect(created.agent_id).toBe('agent-1');
    expect(created.credentialValues.token).toBe('');

    const account = {
      account_id: 'acc', channel_type: 'signal', display_name: 'My Signal', enabled: true, agent_id: 'agent-1',
      config: { use_tls: false }, credential_refs: {}, allowed_senders: [], dm_policy: 'pairing', group_policy: 'pairing',
      created_at: null, updated_at: null,
    } satisfies ChannelAccount;
    const edited = createChannelDraft(meta, [agent], account);
    expect(edited.display_name).toBe('My Signal');
    expect(edited.settingValues.use_tls).toBe('false');
  });

  it('shows legacy assistant delivery settings as concatenated when editing', () => {
    const deliveryMeta = {
      ...meta,
      setting_fields: [
        {
          name: 'assistant_delivery_mode',
          label: 'Assistant delivery mode',
          description: '',
          field_type: 'select',
          default: 'final_only',
          options: ['final_only', 'concatenated', 'immediate'],
        },
      ],
    } satisfies ChannelMeta;
    const account = {
      account_id: 'acc', channel_type: 'signal', display_name: 'My Signal', enabled: true, agent_id: 'agent-1',
      config: {}, credential_refs: {}, allowed_senders: [], dm_policy: 'pairing', group_policy: 'pairing',
      created_at: null, updated_at: null,
    } satisfies ChannelAccount;

    expect(createChannelDraft(deliveryMeta, [agent], account).settingValues.assistant_delivery_mode).toBe('concatenated');

    const explicitLegacy = {
      ...account,
      config: { assistant_delivery_mode: 'final' },
    } satisfies ChannelAccount;
    expect(createChannelDraft(deliveryMeta, [agent], explicitLegacy).settingValues.assistant_delivery_mode).toBe('concatenated');
  });

  it('normalizes booleans and numbers safely', () => {
    expect(normalizeSettingValue(meta, 'use_tls', 'true')).toBe(true);
    expect(normalizeSettingValue(meta, 'missing', 'abc')).toBe('abc');
  });

  it('derives pending pairing counts and summary placeholders', () => {
    const pairingRequests = [
      { account_id: 'acc', status: 'pending' },
      { account_id: 'acc', status: 'completed' },
    ] as PairingRequest[];
    expect(getPendingPairingCount('acc', pairingRequests)).toBe(1);

    const loading = buildSummaryCards([], [], [], false);
    expect(loading[0].value).toBe('--');

    const ready = buildSummaryCards(
      [{ account_id: 'acc', status: { status: 'connected' } } as ChannelAccount],
      [{} as ChannelContact],
      pairingRequests,
      true,
    );
    expect(ready.find((item) => item.id === 'connected')?.value).toBe('1');
    expect(ready.find((item) => item.id === 'pairing')?.value).toBe('1');
  });

  it('formats remaining expiry in minutes', () => {
    const future = new Date(Date.now() + 2 * 60_000).toISOString();
    expect(formatRemaining(future)).toContain('min left');
  });
});
